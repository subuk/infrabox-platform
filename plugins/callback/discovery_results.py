"""Native Ansible stdout callback: facts only, no credential-bearing task dumps."""
import json
import os
from pathlib import Path
import sys

from ansible.plugins.callback import CallbackBase

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from results import atomic, facts_for_publication, host_identity, diagnostic

DOCUMENTATION = '''
name: discovery_results
type: stdout
short_description: Write native facts and sanitized discovery outcomes
description: Capture the fixed gathering event without printing hostvars or errors.
'''


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'discovery_results'

    def __init__(self):
        super().__init__()
        self.directory = Path(os.environ['PLATFORM_RESULTS_DIR'])
        self.hosts = {}

    def save(self):
        atomic(self.directory / 'hosts.json', list(self.hosts.values()))

    def v2_playbook_on_play_start(self, play):
        try:
            inventory = play.get_variable_manager()._inventory
            selected = inventory.get_hosts(play.hosts)
            seen = set()
            for host in selected:
                record = host_identity(host)
                if record['file'] in seen:
                    raise ValueError('inventory_identity_collision')
                seen.add(record['file'])
                self.hosts[host.name] = record
            self.save()
        except Exception:
            atomic(self.directory / 'callback-error.json', {'reason': 'inventory_identity'})
            # Callback exceptions are swallowed by Ansible. Abort before connecting.
            raise SystemExit(4)

    def capture(self, result, status):
        try:
            record = self.hosts[result._host.name]
            if 'discovery_dmi' in result._task.tags:
                stdout = result._result.get('stdout', '')
                ok = status == 'succeeded' and result._result.get('rc') == 0 and isinstance(stdout, str) and len(stdout) <= 1024 * 1024
                atomic(self.directory / 'dmi' / Path(record['file']).name,
                       {'status': 'succeeded' if ok else 'unavailable', 'stdout': stdout if ok else ''})
                return
            if status == 'succeeded':
                if result._task.action not in ('gather_facts', 'ansible.builtin.gather_facts'):
                    return
                if result._result.get('_ansible_no_log') or result._task.no_log:
                    status = 'failed'
                    record['reason'] = 'facts_suppressed'
                elif not isinstance(result._result.get('ansible_facts'), dict):
                    status = 'failed'
                    record['reason'] = 'missing_facts'
                else:
                    facts = facts_for_publication(result._result['ansible_facts'])
                    if len(json.dumps(facts)) > 8 * 1024 * 1024:
                        raise ValueError('facts_size')
                    atomic(self.directory / record['file'], facts)
            record['status'] = status
            if status != 'succeeded':
                record.setdefault('reason', 'connection_failed' if status == 'unreachable' else 'collection_failed')
                if result._result.get('_ansible_no_log') or result._task.no_log:
                    record['error'] = 'Error details suppressed by Ansible no_log.'
                    self.save()
                    return
                message = str(result._result.get('msg', ''))
                details = [message] if message else []
                for field in ('stderr', 'module_stderr'):
                    if result._result.get(field):
                        details.append(field + ': ' + str(result._result[field]))
                # gather_facts can wrap the actual module failure.
                for name, failure in result._result.get('failed_modules', {}).items():
                    if failure.get('_ansible_no_log'):
                        details.append(str(name) + ': details suppressed by no_log')
                        continue
                    for field in ('msg', 'stderr', 'module_stderr'):
                        if failure.get(field):
                            details.append(str(name) + ' ' + field + ': ' + str(failure[field]))
                record['error'] = diagnostic('\n'.join(details) or 'No error details returned by Ansible.')
                for text, reason in [('Host key verification failed', 'ssh_host_trust'),
                                     ('REMOTE HOST IDENTIFICATION HAS CHANGED', 'ssh_host_trust'),
                                     ('Permission denied', 'ssh_authentication'),
                                     ('timed out', 'connection_timeout'),
                                     ('interpreter', 'target_interpreter'),
                                     ('could not be found', 'runtime_dependency')]:
                    if text in message:
                        record['reason'] = reason
                        break
            self.save()
        except Exception:
            atomic(self.directory / 'callback-error.json', {'reason': 'result_export'})

    def v2_runner_on_ok(self, result):
        self.capture(result, 'succeeded')

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self.capture(result, 'failed')

    def v2_runner_on_unreachable(self, result):
        self.capture(result, 'unreachable')
