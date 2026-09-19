"""Conservative path impact; no Ansible/Jinja interpretation."""
import re
import subprocess

class PolicyError(ValueError):
    pass

def changed_paths(source, base, head):
    if not all(re.fullmatch(r'[0-9a-f]{40}', x or '') for x in (base, head)):
        raise PolicyError('PR base/head must be exact commit SHAs')
    merge = subprocess.run(['git','merge-base',base,head],cwd=source,capture_output=True,text=True,check=True).stdout.strip()
    # --no-renames reports both old and new paths, including moves between roles.
    result = subprocess.run(['git','diff','--name-only','--no-renames','-z',merge,head,'--'],cwd=source,capture_output=True,check=True)
    return result.stdout.decode().rstrip('\0').split('\0') if result.stdout else []

def impact(paths, hosts):
    changed = sorted({p.split('/')[1] for p in paths if p.startswith('roles/') and len(p.split('/')) >= 3})
    assignments = {role: sorted(name for name, data in hosts.items()
                    if data.get('infrabox_roles',{}).get(role,{}).get('enabled') is True) for role in changed}
    missing = [r for r, targets in assignments.items() if not targets]
    if missing:
        raise PolicyError('Changed role(s) ' + ', '.join(missing) +
            ' not assigned to any managed host in NetBox. Assign every changed role before validation.')
    relevant = [p for p in paths if p not in ('README.md','AGENTS.md') and not p.startswith('docs/')]
    only_roles = bool(relevant) and all(re.fullmatch(r'roles/[a-z][a-z0-9_]*/.+',p) for p in relevant)
    return {'scope':'roles' if only_roles else ('all' if relevant else 'none'),
            'changed_roles':changed,
            'hosts':sorted(set(h for values in assignments.values() for h in values)) if only_roles
                    else (sorted(hosts) if relevant else [])}
