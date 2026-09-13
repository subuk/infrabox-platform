"""Bounded native facts publication; never serialize arbitrary host variables."""
import html
import json
import os
from pathlib import Path
import re
import tempfile

STATUSES = ('succeeded', 'failed', 'unreachable', 'not_completed')
EXCLUDED = {'ansible_env', 'ansible_local', 'env', 'local', 'facter', 'ohai'}


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def facts_for_publication(facts):
    return {k: v for k, v in facts.items()
            if k not in EXCLUDED and not k.startswith(('facter_', 'ohai_'))}


def host_identity(host):
    values = host.get_vars()
    pk = values.get('infrabox_netbox_id')
    virtual = values.get('infrabox_netbox_virtual')
    if isinstance(pk, bool) or not str(pk).isdigit() or int(pk) < 1 or not isinstance(virtual, bool):
        raise ValueError('inventory_identity')
    kind = 'vm' if virtual else 'device'
    return {'name': host.name, 'object_type': kind, 'object_id': int(pk),
            'file': f'facts/{kind}-{pk}.json', 'status': 'not_completed'}


def cell(value):
    # Prevent workflow commands/control characters and Markdown/HTML injection.
    text = str(value)[:180]
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', text)
    return html.escape(text).replace('|', '&#124;').replace('`', '&#96;')


def summary(manifest, directory):
    counts = {k: sum(h['status'] == k for h in manifest['hosts']) for k in STATUSES}
    manifest['counts'] = {'selected': len(manifest['hosts']), **counts}
    lines = [f"Discovery: {manifest['outcome']}",
             f"Revision: {manifest['revision']}",
             ' | '.join(f'{k}: {v}' for k, v in manifest['counts'].items()), '',
             '| Host | Status | OS / version | Kernel | Logical CPUs | Memory MiB |',
             '| --- | --- | --- | --- | --- | --- |']
    for host in manifest['hosts']:
        path = Path(directory) / host['file']
        facts = json.loads(path.read_text()) if path.exists() else {}
        def fact(key):
            return facts.get('ansible_' + key, facts.get(key, 'unknown'))
        row = [host['name'], host['status'] + (': ' + host['reason'] if host.get('reason') else ''),
               str(fact('distribution')) + ' ' + str(fact('distribution_version')),
               fact('kernel'), fact('processor_vcpus'), fact('memtotal_mb')]
        lines.append('| ' + ' | '.join(cell(v) for v in row) + ' |')
    lines += ['', 'Download the discovery artifact from this Gitea run.',
              'Environment, local, facter and ohai facts are excluded from publication.']
    return '\n'.join(lines) + '\n'
