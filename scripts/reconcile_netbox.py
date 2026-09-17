#!/usr/bin/env python3
"""Deterministic discovery ownership. Facts are data, never executable policy."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import ipaddress
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import sys

from results import atomic


@dataclass
class Interface:
    name: str
    mac: str | None
    addresses: list[str]
    virtual: bool


@dataclass
class Component:
    slot: str
    profile: str
    manufacturer: str
    model: str
    serial: str
    attributes: dict


@dataclass
class Host:
    platform: str | None = None
    serial: str | None = None
    vcpus: int | None = None
    memory: int | None = None
    architecture: str | None = None
    interfaces: list[Interface] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def clean(value, limit=100):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or any(ord(c) < 32 for c in value):
        return None
    if value.lower() in ('unknown', 'not specified', 'none', 'to be filled by o.e.m.', 'default string', 'not provided'):
        return None
    return value


def positive(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**31 else None


def dmi_components(text, warnings):
    """Read only SMBIOS type 4/17 records and a fixed set of fields."""
    components = []
    for block in re.split(r'(?m)^Handle ', text):
        match = re.search(r'DMI type (4|17),', block)
        if not match:
            continue
        values = {}
        for line in block.splitlines():
            if line.startswith('\t') and ':' in line:
                key, value = line.strip().split(':', 1)
                values[key] = value.strip()
        cpu = match[1] == '4'
        if (cpu and 'Unpopulated' in values.get('Status', '')) or (not cpu and values.get('Size') == 'No Module Installed'):
            continue
        slot = clean(values.get('Socket Designation' if cpu else 'Locator'), 64)
        manufacturer = clean(values.get('Manufacturer'))
        model = clean(values.get('Version' if cpu else 'Part Number'))
        if not all((slot, manufacturer, model)):
            warnings.append('dmi_component_identity_incomplete')
            continue
        attrs = {}
        if cpu:
            cores = values.get('Core Count', '')
            if cores.isdigit() and positive(int(cores)):
                attrs['cores'] = int(cores)
        else:
            size = re.fullmatch(r'([0-9]+) (MB|GB|TB|MiB|GiB|TiB)', values.get('Size', ''))
            if not size:
                warnings.append('dmi_memory_capacity_unknown')
                continue
            attrs['capacity_mib'] = int(size[1]) * {'MB': 1, 'MiB': 1, 'GB': 1024, 'GiB': 1024, 'TB': 1048576, 'TiB': 1048576}[size[2]]
            kind = clean(values.get('Type'))
            if kind:
                attrs['technology'] = kind
        components.append(Component(slot, 'CPU' if cpu else 'RAM', manufacturer, model,
                                    clean(values.get('Serial Number'), 50) or '', attrs))
    slots = [c.slot for c in components]
    if len(set(slots)) != len(slots):
        warnings.append('dmi_slot_ambiguous')
        return []
    return components


def normalize(facts, kind, dmi=None):
    def fact(name):
        return facts.get('ansible_' + name, facts.get(name))
    out = Host()
    distro, version = clean(fact('distribution'), 60), clean(fact('distribution_version'), 30)
    if distro and version:
        out.platform = distro + ' ' + version
    out.architecture = clean(fact('architecture'), 40)
    out.serial = clean(fact('product_serial'), 50)
    if kind == 'vm':
        out.vcpus, out.memory = positive(fact('processor_vcpus')), positive(fact('memtotal_mb'))
    excluded = os.environ.get('PLATFORM_INTERFACE_EXCLUDE_PATTERNS', 'cilium_*,lxc*')
    patterns = [p.strip() for p in excluded.split(',') if p.strip()]
    for name in fact('interfaces') or []:
        if not clean(name, 64) or name == 'lo':
            continue
        if any(fnmatchcase(name, pattern) for pattern in patterns):
            continue
        data = fact(name)
        if not isinstance(data, dict):
            continue
        mac = clean(data.get('macaddress'), 17)
        if mac and (not re.fullmatch(r'[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}', mac) or mac == '00:00:00:00:00:00'):
            mac = None
            out.warnings.append('invalid_mac:' + name)
        addresses = []
        entries = [data.get('ipv4', {})] + data.get('ipv4_secondaries', []) + data.get('ipv6', [])
        for entry in entries:
            if not entry.get('address'):
                continue
            try:
                prefix = entry.get('prefix', entry.get('netmask'))
                if prefix is None:
                    raise ValueError()
                address = ipaddress.ip_interface(str(entry['address']) + '/' + str(prefix))
                if not (address.ip.is_loopback or address.ip.is_link_local or address.ip.is_multicast or address.ip.is_unspecified):
                    addresses.append(str(address))
            except ValueError:
                out.warnings.append('invalid_ip:' + name)
        out.interfaces.append(Interface(name, mac.upper() if mac else None, sorted(set(addresses)), data.get('type') in ('bridge', 'bonding', 'vlan', 'tun')))
    if kind == 'device':
        # A wrongly classified guest must not manufacture physical modules.
        if fact('virtualization_role') == 'guest':
            out.warnings.append('device_is_virtual_guest')
        elif dmi and dmi.get('status') == 'succeeded':
            try:
                out.components = dmi_components(dmi['stdout'], out.warnings)
            except (TypeError, ValueError, KeyError):
                out.warnings.append('dmi_parse_failed')
        else:
            out.warnings.append('dmi_unavailable' if dmi else 'dmi_not_collected')
    return out


def ident(value):
    return getattr(value, 'id', value)


def slug(name):
    # Stable, collision-resistant identity for discovered catalog entries.
    import hashlib
    return re.sub('[^a-z0-9]+', '-', name.lower()).strip('-')[:65] + '-' + hashlib.sha256(name.encode()).hexdigest()[:10]


class Reconciler:
    def __init__(self, api, managed_tag='infrabox-managed'):
        self.api, self.managed_tag = api, managed_tag
        self.changes = []
        self.warnings = []

    def patch(self, obj, desired, label):
        diff = {}
        for key, value in desired.items():
            old = getattr(obj, key, None)
            if key == 'custom_fields':
                value = {**(old or {}), **value}
                previous = (old or {}).get('discovery_last_success')
                current = value.get('discovery_last_success')
                if isinstance(previous, str) and isinstance(current, str):
                    try:
                        if datetime.fromisoformat(previous.replace('Z', '+00:00')) == datetime.fromisoformat(current.replace('Z', '+00:00')):
                            value['discovery_last_success'] = previous
                    except ValueError:
                        pass
            if ident(old) != value:
                diff[key] = value
        if diff:
            obj.update(diff)
            self.changes.append({'object': label, 'id': obj.id, 'fields': sorted(diff)})

    def create(self, endpoint, data, label):
        obj = endpoint.create(data)
        self.changes.append({'object': label, 'id': obj.id, 'fields': sorted(data), 'created': True})
        return obj

    def catalog(self, endpoint, name, label, **extra):
        matches = list(endpoint.filter(name=name))
        if len(matches) > 1:
            raise ValueError('ambiguous_catalog_' + label)
        return matches[0] if matches else self.create(endpoint, {'name': name, 'slug': slug(name), **extra}, label)

    def apply(self, record, host, observation):
        api = self.api
        vm = record['object_type'] == 'vm'
        endpoint = api.virtualization.virtual_machines if vm else api.dcim.devices
        obj = endpoint.get(record['object_id'])
        if obj is None or obj.name != record['name'] or self.managed_tag not in [t.slug for t in obj.tags]:
            raise ValueError('host_identity_changed')
        # Recheck exact native name uniqueness: Ansible merges equal names.
        matches = sum(len(list(e.filter(name=record['name'], tag=self.managed_tag))) for e in (api.dcim.devices, api.virtualization.virtual_machines))
        if matches != 1:
            raise ValueError('host_identity_ambiguous')
        desired = {'custom_fields': {}}
        if host.architecture:
            desired['custom_fields']['discovery_architecture'] = host.architecture
        if host.platform:
            platforms = list(api.dcim.platforms.filter(name=host.platform))
            compatible = [p for p in platforms if not p.manufacturer]
            if len(compatible) == 1:
                desired['platform'] = compatible[0].id
            elif not platforms:
                desired['platform'] = self.create(api.dcim.platforms, {'name': host.platform, 'slug': slug(host.platform)}, 'platform').id
            else:
                self.warnings.append('platform_ambiguous')
        if host.serial:
            desired['serial'] = host.serial
        if vm:
            for key in ('vcpus', 'memory'):
                if getattr(host, key) is not None:
                    desired[key] = getattr(host, key)
        self.patch(obj, desired, record['object_type'])
        for interface in host.interfaces:
            self.interface(obj, vm, interface)
        if not vm:
            for component in host.components:
                self.component(obj, component)
        # Freshness is separate from effective infrastructure changes.
        before = len(self.changes)
        self.patch(obj, {'custom_fields': observation}, 'provenance')
        provenance_changed = len(self.changes) > before
        if provenance_changed:
            self.changes.pop()
        return provenance_changed

    def interface(self, host, vm, data):
        api = self.api
        endpoint = api.virtualization.interfaces if vm else api.dcim.interfaces
        parent = 'virtual_machine' if vm else 'device'
        matches = list(endpoint.filter(**{parent + '_id': host.id, 'name': data.name}))
        if len(matches) > 1:
            self.warnings.append('interface_ambiguous:' + data.name)
            return
        interface = matches[0] if matches else self.create(endpoint, {parent: host.id, 'name': data.name,
            **({} if vm else {'type': 'virtual' if data.virtual else 'other'})}, 'interface')
        object_type = 'virtualization.vminterface' if vm else 'dcim.interface'
        if data.mac:
            macs = list(api.dcim.mac_addresses.filter(mac_address=data.mac))
            owned = [m for m in macs if m.assigned_object_type == object_type and m.assigned_object_id == interface.id]
            if len(owned) == 1:
                self.patch(interface, {'primary_mac_address': owned[0].id}, 'interface')
            elif not macs:
                mac = self.create(api.dcim.mac_addresses, {'mac_address': data.mac, 'assigned_object_type': object_type,
                    'assigned_object_id': interface.id}, 'mac')
                self.patch(interface, {'primary_mac_address': mac.id}, 'interface')
            else:
                self.warnings.append('mac_conflict:' + data.name)
        for address in data.addresses:
            # Interface VRF is the explicit context. Never infer it from address ranges.
            vrf = ident(getattr(interface, 'vrf', None))
            matches = list(api.ipam.ip_addresses.filter(address=str(ipaddress.ip_interface(address).ip)))
            same = [ip for ip in matches if ident(ip.vrf) == vrf and ip.assigned_object_type == object_type and ip.assigned_object_id == interface.id]
            if len(same) == 1:
                continue
            if matches:
                self.warnings.append('ip_conflict:' + address)
                continue
            if vrf is None:
                # An explicit existing global-scope IP on this interface establishes global VRF.
                existing = list(api.ipam.ip_addresses.filter(**{'vminterface_id' if vm else 'interface_id': interface.id}))
                if not any(ip.vrf is None for ip in existing):
                    self.warnings.append('ip_vrf_unknown:' + address)
                    continue
            self.create(api.ipam.ip_addresses, {'address': address, 'vrf': vrf, 'assigned_object_type': object_type,
                'assigned_object_id': interface.id}, 'ip')

    def component(self, device, data):
        api = self.api
        bays = list(api.dcim.module_bays.filter(device_id=device.id, name=data.slot))
        if len(bays) > 1:
            self.warnings.append('module_bay_ambiguous:' + data.slot)
            return
        bay = bays[0] if bays else self.create(api.dcim.module_bays, {'device': device.id, 'name': data.slot}, 'module_bay')
        profile = api.dcim.module_type_profiles.get(name='Discovery ' + data.profile)
        if profile is None:
            raise ValueError('discovery_profile_missing')
        manufacturer = self.catalog(api.dcim.manufacturers, data.manufacturer, 'manufacturer')
        types = list(api.dcim.module_types.filter(manufacturer_id=manufacturer.id, model=data.model))
        if len(types) > 1:
            self.warnings.append('module_type_ambiguous:' + data.slot)
            return
        if types:
            module_type = types[0]
            if ident(module_type.profile) != profile.id or any(dict(module_type.attributes).get(k) != v for k, v in data.attributes.items()):
                self.warnings.append('module_type_conflict:' + data.slot)
                return
        else:
            module_type = self.create(api.dcim.module_types, {'manufacturer': manufacturer.id, 'model': data.model,
                'profile': profile.id, 'attributes': data.attributes}, 'module_type')
        modules = list(api.dcim.modules.filter(device_id=device.id, module_bay_id=bay.id))
        desired = {'module_type': module_type.id}
        if data.serial:
            desired['serial'] = data.serial
        if len(modules) > 1:
            self.warnings.append('module_ambiguous:' + data.slot)
        elif modules:
            # Replacement requires an operator decision: no silent retirement of an asset.
            current = modules[0]
            if ident(current.module_type) != module_type.id or (data.serial and current.serial and current.serial != data.serial):
                self.warnings.append('module_replacement:' + data.slot)
            else:
                self.patch(current, desired, 'module')
        else:
            self.create(api.dcim.modules, {'device': device.id, 'module_bay': bay.id, 'status': 'active', 'replicate_components': False, 'adopt_components': False, **desired}, 'module')


def reconcile(directory, api, dependency_error=None):
    directory = Path(directory)
    manifest = json.loads((directory / 'run.json').read_text())
    results = []
    observation = {'discovery_last_success': manifest['finished_at'], 'discovery_source': 'ansible',
        'discovery_run': str(manifest['run_id']) + '/' + str(manifest['attempt']), 'discovery_revision': manifest['revision']}
    for record in manifest['hosts']:
        result = {k: record[k] for k in ('name', 'object_type', 'object_id', 'status')}
        result.update(reconciliation_status='skipped', changes=[], warnings=[], errors=[], provenance_changed=False)
        worker = Reconciler(api, os.environ.get('PLATFORM_MANAGED_TAG', 'infrabox-managed'))
        if record['status'] == 'succeeded':
            try:
                if dependency_error:
                    raise RuntimeError(dependency_error)
                kind, pk = record['object_type'], record['object_id']
                if kind not in ('device', 'vm') or type(pk) is not int or pk < 1:
                    raise ValueError('invalid_host_identity')
                path = directory / 'facts' / f'{kind}-{pk}.json'
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError('facts_too_large')
                dmi_path = directory / 'dmi' / f'{kind}-{pk}.json'
                dmi = json.loads(dmi_path.read_text()) if dmi_path.exists() else None
                host = normalize(json.loads(path.read_text()), kind, dmi)
                result['warnings'] = host.warnings
                result['provenance_changed'] = worker.apply(record, host, observation)
                result['reconciliation_status'] = 'succeeded'
            except Exception as error:
                result['reconciliation_status'] = 'failed'
                # HTTP bodies/URLs can contain credentials; report only a fixed class/status.
                status = getattr(getattr(error, 'req', None), 'status_code', None)
                reason = 'netbox_auth' if status in (401, 403) else 'netbox_api' if status or type(error).__module__.startswith('requests.') else 'reconciliation_error'
                if dependency_error:
                    reason = dependency_error
                elif type(error) is ValueError and str(error) in ('host_identity_changed','host_identity_ambiguous','invalid_host_identity','facts_too_large','discovery_profile_missing'):
                    reason = str(error)
                result['errors'].append(reason)
                if isinstance(status, int):
                    result['errors'].append('netbox_http_' + str(status))
            result['changes'] = worker.changes
            result['warnings'] += worker.warnings
        else:
            result['errors'] = [record.get('reason', 'collection_failed')]
        result['warnings'] = sorted(set(result['warnings']))
        results.append(result)
    counts = {'selected': len(results), 'collected': sum(h['status'] == 'succeeded' for h in results),
        'reconciled': sum(h['reconciliation_status'] == 'succeeded' for h in results),
        'changed': sum(bool(h['changes']) for h in results),
        'unchanged': sum(h['reconciliation_status'] == 'succeeded' and not h['changes'] for h in results),
        'warning': sum(bool(h['warnings']) for h in results),
        'failed': sum(h['status'] != 'succeeded' or h['reconciliation_status'] == 'failed' for h in results),
        'unreachable': sum(h['status'] == 'unreachable' for h in results)}
    report = {**{k: manifest[k] for k in ('run_id', 'attempt', 'revision', 'pattern', 'started_at', 'finished_at', 'ansible_return_code')},
        'schema_version': 2, 'collection_outcome': manifest['outcome'], 'hosts': results, 'counts': counts,
        'outcome': 'success' if manifest['outcome'] == 'success' and not counts['failed'] else 'failed'}
    atomic(directory / 'reconciliation-summary.json', report)
    return report


def main():
    import pynetbox
    import requests
    from discover import bao

    class VerifiedSession(requests.Session):
        def request(self, method, url, **kwargs):
            if not url.startswith('https://'):
                raise ValueError('netbox_tls_required')
            kwargs['timeout'] = 30
            kwargs['allow_redirects'] = False
            return super().request(method, url, **kwargs)

    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / 'run.json').exists():
        timestamp = datetime.now(timezone.utc).isoformat()
        atomic(directory / 'run.json', {'run_id': os.environ['GITEA_RUN_ID'], 'attempt': os.environ.get('GITEA_RUN_ATTEMPT','1'),
            'revision': os.environ['GITEA_SHA'], 'pattern': os.environ.get('PLATFORM_TARGETS',''), 'started_at': timestamp,
            'finished_at': timestamp, 'ansible_return_code': 1, 'hosts': [], 'outcome': 'dependency_failed'})
    api, error = None, None
    try:
        token = bao(os.environ.get('PLATFORM_KV_MOUNT', 'kv') + '/data/platform/netbox')['token']
        api = pynetbox.api(os.environ['NETBOX_API'])
        api.http_session = VerifiedSession()
        api.http_session.verify = os.environ['SSL_CERT_FILE']
        api.http_session.trust_env = False
        # Session-only credential: pynetbox 7.5's Token header is incompatible with v2 tokens.
        api.http_session.headers['Authorization'] = 'Bearer ' + token
    except Exception:
        error = 'credential_unavailable'
    report = reconcile(directory, api, error)
    print(json.dumps({'outcome': report['outcome'], 'counts': report['counts']}))
    return 0 if report['outcome'] == 'success' else 1


if __name__ == '__main__':
    sys.exit(main())
