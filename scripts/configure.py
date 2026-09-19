#!/usr/bin/env python3
"""Trusted configure entry point. No user-supplied Ansible command arguments."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import yaml
from jsonschema import Draft202012Validator
from discover import bao, pattern_argument
from impact import PolicyError, changed_paths, impact
from results import atomic, diagnostic
from runtime_contract import fingerprint

def boolean(value, default):
    if value in (None, ''): return default
    if value is True or value == 'true': return True
    if value is False or value == 'false': return False
    raise PolicyError('Execution booleans must be true or false')

def policy(event_name, event, env, revision, deployed):
    if event_name == 'pull_request':
        pr=event['pull_request']
        if pr['head']['repo']['full_name'] != env['GITEA_REPOSITORY'] or pr['base']['repo']['full_name'] != env['GITEA_REPOSITORY']:
            raise PolicyError('Fork PRs cannot use the credential-bearing Platform runner')
        if revision != pr['head']['sha']:
            raise PolicyError('Checkout differs from the reviewed PR head')
        return {'check':True,'diff':True,'base':pr['base']['sha'],'head':revision,'limit':None}
    if event_name != 'workflow_dispatch':
        raise PolicyError('Only pull_request and workflow_dispatch are supported')
    if revision != deployed:
        raise PolicyError('Manual execution requires the deployed approved revision')
    inputs=event.get('inputs') or {}
    check=boolean(inputs.get('check',env.get('PLATFORM_CHECK')),True)
    return {'check':check,'diff':boolean(inputs.get('diff',env.get('PLATFORM_DIFF')),check),
            'limit':inputs.get('limit',env.get('PLATFORM_LIMIT')) or 'all'}

def registry(source):
    order=json.loads((source/'role-order.json').read_text())
    available={p.name for p in (source/'roles').iterdir() if p.is_dir()}
    if not isinstance(order,list) or not all(isinstance(r,str) and re.fullmatch('[a-z][a-z0-9_]*',r) for r in order) or len(order)!=len(set(order)) or set(order)!=available:
        raise PolicyError('role-order.json must list every implemented role exactly once')
    for role in order:
        for file in ('defaults/main.yml','tasks/main.yml','tasks/validate.yml'):
            if not (source/'roles'/role/file).is_file(): raise PolicyError('Incomplete role: '+role)
    return order

def validate_hosts(hosts, order, schema):
    Draft202012Validator.check_schema(schema)
    validator=Draft202012Validator(schema)
    identities=set()
    for name,data in hosts.items():
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*',name) or name in ('all','ungrouped','localhost'):
            raise PolicyError('Inventory host name cannot safely form an exact limit')
        identity=(data.get('infrabox_netbox_virtual'),data.get('infrabox_netbox_id'))
        if type(identity[0]) is not bool or type(identity[1]) is not int or identity[1]<1 or identity in identities:
            raise PolicyError('Missing or duplicate NetBox identity for '+name)
        identities.add(identity)
        roles=data.get('infrabox_roles',{})
        if not isinstance(roles,dict) or set(roles)-set(order): raise PolicyError('Unknown or invalid infrabox_roles on '+name)
        # NetBox metadata and native connection variables are not role inputs.
        public={k:v for k,v in data.items() if k=='infrabox_roles' or k.startswith(tuple(r+'_' for r in order))}
        errors=list(validator.iter_errors(public))
        if errors:
            e=errors[0]
            raise PolicyError('Invalid configuration on '+name+' at '+'.'.join(map(str,e.absolute_path))+' ('+e.validator+')')
        reserved=[k for k in data if k.startswith('infrabox_') and k not in ('infrabox_roles','infrabox_netbox_id','infrabox_netbox_virtual')]
        if reserved: raise PolicyError('Reserved orchestration input on '+name)

def snapshot_inventory(inventory):
    """Convert native --list output to a private static snapshot preserving groups."""
    hosts=inventory['_meta']['hostvars']
    groups={name:{'hosts':{h:{} for h in data.get('hosts',[])},
                  'children':{g:{} for g in data.get('children',[])},'vars':data.get('vars',{})}
            for name,data in inventory.items() if name!='_meta'}
    groups.setdefault('all',{})['hosts']=hosts
    return groups

def execute(argv,source,env,timeout=900):
    with tempfile.TemporaryFile() as output:
        process=subprocess.Popen(argv,cwd=source,env=env,stdout=output,stderr=output,start_new_session=True)
        try:
            code=process.wait(timeout=timeout)
        finally:
            try: os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL); process.wait()
        output.seek(0)
        text=output.read(4*1024*1024).decode(errors='replace')
    return code,text

def run():
    os.umask(0o077)
    source=Path.cwd().resolve()
    results=Path(os.environ['PLATFORM_CONFIGURE_RESULTS_DIR'])
    results.mkdir(parents=True,exist_ok=True,mode=0o700)
    report={'outcome':'failed','run_id':os.environ['GITEA_RUN_ID'],
            'attempt':os.environ.get('GITEA_RUN_ATTEMPT','1'),
            'started_at':datetime.now(timezone.utc).isoformat()}
    credentials=[]
    try:
        revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
        event=json.loads(Path(os.environ.get('GITEA_EVENT_PATH') or os.environ['GITHUB_EVENT_PATH']).read_text())
        selected=policy(os.environ['GITEA_EVENT_NAME'],event,os.environ,revision,Path('/opt/platform/REVISION').read_text().strip())
        runtime=fingerprint(source)
        if runtime!=Path('/opt/platform/RUNTIME_FINGERPRINT').read_text().strip():
            raise PolicyError('Incompatible runtime: provision the candidate runtime through Core before retrying')
        report.update(revision=revision,runtime_fingerprint=runtime,mode='check' if selected['check'] else 'apply',diff=selected['diff'])
        order=registry(source)
        schema=json.loads((source/'schemas/config-context.schema.json').read_text())
        with tempfile.TemporaryDirectory(prefix='configure-',dir=os.environ['PLATFORM_PRIVATE_DIR']) as temp:
            env=os.environ.copy()
            env.update(ANSIBLE_CONFIG=str(source/'ansible.cfg'),ANSIBLE_STDOUT_CALLBACK='configure_results',
                       ANSIBLE_LOCAL_TEMP=temp+'/ansible',PLATFORM_CONFIGURE_RESULTS_DIR=str(results))
            mount=env.get('PLATFORM_KV_MOUNT','kv')
            token=bao(mount+'/data/platform/netbox')['token']
            credentials.append(token);env['NETBOX_TOKEN']=token
            code,output=execute(['ansible-inventory','-i',str(source/'inventory/netbox.yml'),'--list','--export'],source,env)
            if code: raise PolicyError('NetBox inventory loading failed')
            try: inventory=json.loads(output)
            except ValueError: raise PolicyError('NetBox inventory returned diagnostics or invalid output') from None
            hosts=inventory.get('_meta',{}).get('hostvars',{})
            if not hosts: raise PolicyError('NetBox inventory has no managed hosts')
            # Validate assignment structure globally before role impact, role inputs only in final scope.
            for name,data in hosts.items():
                roles=data.get('infrabox_roles',{})
                if not isinstance(roles,dict) or any(not isinstance(v,dict) or type(v.get('enabled')) is not bool for v in roles.values()):
                    raise PolicyError('Invalid role assignments on '+name)
            snapshot=Path(temp)/'inventory.yml'
            snapshot.write_text(yaml.safe_dump(snapshot_inventory(inventory)))
            if selected['limit'] is None:
                scope=impact(changed_paths(source,selected['base'],selected['head']),hosts)
                report.update(base=selected['base'],impact=scope)
                names=scope['hosts']
                if scope['scope']=='none':
                    report['outcome']='not_required';return 0
            else:
                # Use native inventory pattern resolution, no SSH or execution.
                from ansible.parsing.dataloader import DataLoader
                from ansible.inventory.manager import InventoryManager
                pattern_argument(selected['limit'])
                manager=InventoryManager(loader=DataLoader(),sources=[str(snapshot)])
                names=sorted(h.name for h in manager.get_hosts(selected['limit']))
                report['impact']={'scope':'manual','requested_limit':selected['limit']}
            if not names: raise PolicyError('Selected scope has no managed hosts')
            validate_hosts({n:hosts[n] for n in names},order,schema)
            report['hosts']=names;report['roles']=order
            key=bao(mount+'/data/platform/ssh/default')['private_key']
            credentials.append(key.strip())
            keypath=Path(temp)/'ssh-key';keypath.write_text(key.rstrip()+'\n');keypath.chmod(0o600)
            env.update(ANSIBLE_PRIVATE_KEY_FILE=str(keypath),ANSIBLE_SSH_ARGS='-C -o ControlMaster=auto -o ControlPersist=60 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/run/platform/ssh-trust/known_hosts')
            args=['ansible-playbook','site.yml','-i',str(snapshot),'--limit='+','.join(names)]
            if selected['check']: args.append('--check')
            if selected['diff']: args.append('--diff')
            code,output=execute(args,source,env)
            for value in credentials: output=output.replace(value,'[REDACTED]')
            output=diagnostic(output,limit=262144)
            print(output,flush=True)
            (results/'ansible.log').write_text(output)
            report['ansible_return_code']=code
            stats=results/'stats.json'
            if stats.exists(): report['stats']=json.loads(stats.read_text())
            if code or not stats.exists() or (results/'callback-error.json').exists():
                raise PolicyError('Ansible preflight/check/apply failed; inspect sanitized task results')
            report['outcome']='success'
            return 0
    except Exception as error:
        report['reason']=str(error) if isinstance(error,PolicyError) else type(error).__name__
        print(report['reason'],flush=True)
        return 1
    finally:
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        atomic(results/'run.json',report)
        print(json.dumps(report),flush=True)

if __name__=='__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    sys.exit(run())
