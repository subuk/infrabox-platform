"""Persist compact run health outside disposable job workspaces."""
import json
import os
from pathlib import Path
import sys
import time
from results import atomic

path = Path('/data/discovery-health.json')
old = json.loads(path.read_text()) if path.exists() else {}
if sys.argv[1] == 'start':
    value = {**old, 'run_id': os.environ['GITEA_RUN_ID'], 'started': time.time(), 'running': True,
             'last_success': old.get('last_success', 0)}
elif sys.argv[1] == 'complete':
    value = {**old, 'finished': time.time(), 'workflow_success': old.get('workflow_success', False) and os.environ.get('PLATFORM_JOB_STATUS') == 'success'}
else:
    value = {**old, 'running': False, 'finished': time.time(), 'workflow_success': os.environ.get('PLATFORM_JOB_STATUS') == 'success'}
    report = Path(os.environ['PLATFORM_RESULTS_DIR']) / 'reconciliation-summary.json'
    if report.exists():
        report = json.loads(report.read_text())
        value['counts'] = report['counts']
        value['reconciliation_failures'] = sum(h['reconciliation_status'] == 'failed' for h in report['hosts'])
        value['api_failures'] = sum(any(e in ('netbox_api', 'netbox_auth') for e in h['errors']) for h in report['hosts'])
        value['auth_failures'] = sum('netbox_auth' in h['errors'] for h in report['hosts'])
        if report['outcome'] == 'success':
            value['last_success'] = time.time()
        value['workflow_success'] &= report['outcome'] == 'success'
    else:
        value['workflow_success'] = False
atomic(path, value)
