#!/usr/bin/env python3
"""Fixed Ansible discovery lifecycle; connection data belongs to native inventory."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError
import ssl

from results import atomic, summary


class DiscoveryError(ValueError):
    """Only fixed reason codes, safe to publish in the run manifest."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise DiscoveryError('openbao_redirect_refused')


def now():
    return datetime.now(timezone.utc).isoformat()


def pattern_argument(value):
    value = value.strip() or 'all'
    if len(value) > 4096 or '\x00' in value or '\n' in value or '\r' in value or value.startswith('@'):
        raise DiscoveryError('invalid_pattern')
    return '--limit=' + value


def bao(path):
    address = os.environ['VAULT_ADDR'].rstrip('/')
    if not address.startswith('https://'):
        raise DiscoveryError('openbao_tls_required')
    token = Path(os.environ['PLATFORM_BAO_TOKEN_FILE']).read_text().strip()
    request = Request(address + '/v1/' + path,
                      headers={'X-Vault-Token': token})
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(
            context=ssl.create_default_context(cafile=os.environ['SSL_CERT_FILE'])))
        with opener.open(request, timeout=15) as response:
            return json.load(response)['data']['data']
    except HTTPError as error:
        raise DiscoveryError('openbao_http_' + str(error.code)) from None


def run(directory, source, pattern, revision, timeout=900, inventory=None):
    directory, source = Path(directory), Path(source)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    manifest = {'run_id': os.environ.get('GITEA_RUN_ID', 'local'),
                'attempt': os.environ.get('GITEA_RUN_ATTEMPT', '1'),
                'started_at': now(), 'revision': revision, 'pattern': pattern,
                'hosts': [], 'outcome': 'pending'}
    process = None
    rc = 1
    diagnostic_reason = None
    try:
        built = Path('/opt/platform/REVISION')
        if built.exists() and built.read_text().strip() != revision:
            raise DiscoveryError('runtime_revision_mismatch')
        argument = pattern_argument(pattern)
        with tempfile.TemporaryDirectory(prefix='credentials-', dir=os.environ.get('PLATFORM_PRIVATE_DIR')) as temp:
            env = os.environ.copy()
            env.update(ANSIBLE_CONFIG=str(source / 'ansible.cfg'),
                       PLATFORM_RESULTS_DIR=str(directory), ANSIBLE_LOCAL_TEMP=temp + '/ansible')
            if inventory is None:
                mount = os.environ.get('PLATFORM_KV_MOUNT', 'kv')
                token = bao(mount + '/data/platform/netbox').get('token')
                if not isinstance(token, str) or not token.strip():
                    raise DiscoveryError('netbox_token_missing')
                env['NETBOX_TOKEN'] = token
                key = bao(mount + '/data/platform/ssh/default').get('private_key')
                if not isinstance(key, str) or not key.strip():
                    raise DiscoveryError('ssh_private_key_missing')
                key_path = Path(temp) / 'ssh-key'
                key_path.write_text(key.rstrip() + '\n')
                key_path.chmod(0o600)
                env['ANSIBLE_PRIVATE_KEY_FILE'] = str(key_path)
                env['ANSIBLE_SSH_ARGS'] = '-C -o ControlMaster=auto -o ControlPersist=60 -o UserKnownHostsFile=/run/platform/trust/known_hosts'
            args = ['ansible-playbook', str(source / 'playbooks/discover.yml'), argument]
            if inventory is not None:
                args += ['-i', str(inventory)]
            # Raw errors can include templated context/credentials. Keep them private
            # only while the child runs; export bounded reason codes instead.
            with tempfile.TemporaryFile() as diagnostics:
                process = subprocess.Popen(args, cwd=source, env=env, stdout=diagnostics,
                                           stderr=diagnostics, start_new_session=True)
                try:
                    rc = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    manifest['outcome'] = 'timeout'
                    rc = 124
                finally:
                    # Also remove persistent SSH ControlMaster descendants.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    if process.poll() is None:
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                diagnostics.seek(0)
                message = diagnostics.read(1024 * 1024).decode(errors='replace')
                if 'CERTIFICATE_VERIFY_FAILED' in message:
                    diagnostic_reason = 'tls_verification_failed'
                elif ('Permission denied:' in message and 'inventory plugin' in message) or '401' in message or '403' in message:
                    diagnostic_reason = 'inventory_authentication_failed'
                elif 'leaves us with no hosts to target' in message:
                    diagnostic_reason = 'no_targets'
    except (Exception, KeyboardInterrupt) as error:
        manifest['outcome'] = 'dependency_failed'
        manifest['reason'] = str(error) if isinstance(error, DiscoveryError) else type(error).__name__
        # Never propagate raw HTTP bodies, credential values, or library diagnostics.
    finally:
        hosts_path = directory / 'hosts.json'
        if hosts_path.exists():
            manifest['hosts'] = json.loads(hosts_path.read_text())
        if manifest['outcome'] == 'pending':
            if (directory / 'callback-error.json').exists():
                manifest['outcome'] = 'result_export_failed'
            elif diagnostic_reason in ('tls_verification_failed', 'inventory_authentication_failed'):
                # nb_inventory may deliberately return an empty endpoint on HTTP 403.
                # Preserve any captured facts but never call that inventory complete.
                manifest['outcome'] = 'inventory_failed'
                manifest['reason'] = diagnostic_reason
            elif not manifest['hosts']:
                manifest['outcome'] = 'no_targets' if rc == 0 or diagnostic_reason == 'no_targets' else 'inventory_failed'
                if diagnostic_reason:
                    manifest['reason'] = diagnostic_reason
            elif rc or any(h['status'] != 'succeeded' for h in manifest['hosts']):
                manifest['outcome'] = 'partial_failure' if any(h['status'] == 'succeeded' for h in manifest['hosts']) else 'collection_failed'
            else:
                manifest['outcome'] = 'success'
        manifest['finished_at'] = now()
        manifest['ansible_return_code'] = rc
        report = summary(manifest, directory)
        atomic(directory / 'run.json', manifest)
        (directory / 'summary.md').write_text(report)
        print(report, flush=True)
    return 0 if manifest['outcome'] == 'success' else 1


if __name__ == '__main__':
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    sys.exit(run(os.environ['PLATFORM_RESULTS_DIR'], Path(__file__).resolve().parents[1],
                 os.environ.get('PLATFORM_TARGETS', ''), os.environ['GITEA_SHA'],
                 int(os.environ.get('PLATFORM_DISCOVERY_TIMEOUT', '900'))))
