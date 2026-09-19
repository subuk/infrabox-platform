"""Bounded configuration output honoring no_log, without hostvars/module dumps."""
import json
import os
from pathlib import Path
import sys
from ansible.plugins.callback import CallbackBase
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from results import atomic, diagnostic
DOCUMENTATION="""
name: configure_results
type: stdout
short_description: Sanitized configure tasks, diffs and recap
description: Show planned/applied changes without publishing raw inventory.
"""
class CallbackModule(CallbackBase):
    CALLBACK_VERSION=2.0
    CALLBACK_TYPE='stdout'
    CALLBACK_NAME='configure_results'
    def __init__(self):
        super().__init__()
        self.directory=Path(os.environ['PLATFORM_CONFIGURE_RESULTS_DIR'])
    def show(self,result,status):
        if result._task.no_log or result._result.get('_ansible_no_log'):
            self._display.display(status+': [no_log]');return
        text=status+' '+result._host.name+': '+result._task.get_name()
        if status in ('failed','unreachable'):
            text+=' '+diagnostic(str(result._result.get('msg','')),limit=2048)
        if result._result.get('changed') and self._display.verbosity>=0:
            text+=' [changed]'
        self._display.display(text)
    def v2_runner_on_ok(self,result): self.show(result,'ok')
    def v2_runner_on_failed(self,result,ignore_errors=False): self.show(result,'failed')
    def v2_runner_on_unreachable(self,result): self.show(result,'unreachable')
    def v2_runner_on_skipped(self,result): pass
    def v2_on_file_diff(self,result):
        if result._task.no_log or result._result.get('_ansible_no_log'): return
        self._display.display(diagnostic(self._get_diff(result._result.get('diff',[])),limit=32768))
    def v2_playbook_on_stats(self,stats):
        atomic(self.directory/'stats.json',{h:stats.summarize(h) for h in sorted(stats.processed)})
