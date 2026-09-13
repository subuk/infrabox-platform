import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REVISION = Path('/opt/platform/REVISION').read_text().strip() if Path('/opt/platform/REVISION').exists() else '0' * 40
sys.path.insert(0, str(ROOT / 'scripts'))
from discover import pattern_argument, run
from results import facts_for_publication, summary


class DiscoveryTests(unittest.TestCase):
    def test_patterns_remain_native_single_argument(self):
        for pattern in ['all', 'web*:!db', 'web:&production', '~web[0-9]+', 'one,two', '--help', '$(touch /tmp/no)']:
            self.assertEqual(pattern_argument(pattern), '--limit=' + pattern)
        self.assertEqual(pattern_argument(' '), '--limit=all')
        for pattern in ['@/etc/passwd', 'a\nb', 'a\x00b', 'a' * 4097]:
            with self.assertRaises(ValueError):
                pattern_argument(pattern)

    def test_sensitive_fact_families_are_excluded(self):
        value = facts_for_publication({'ansible_env': {'TOKEN': 'sentinel'}, 'ansible_local': {'secret': 'sentinel'},
                                       'facter_token': 'sentinel', 'ohai_secret': 'sentinel', 'ansible_kernel': 'native'})
        self.assertEqual(value, {'ansible_kernel': 'native'})

    def test_library_exception_does_not_publish_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result'
            with patch('discover.bao', side_effect=ValueError('private-key-sentinel')), contextlib.redirect_stdout(io.StringIO()):
                rc = run(output, ROOT, 'all', REVISION)
            self.assertEqual(rc, 1)
            report = (output / 'run.json').read_text()
            self.assertNotIn('private-key-sentinel', report)
            self.assertEqual(json.loads(report)['reason'], 'ValueError')

    def test_native_selection_and_facts(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            inventory = temp / 'inventory.json'
            inventory.write_text(json.dumps({'all': {'children': {
                'test_good': {'hosts': {'alpha': {'ansible_connection': 'local', 'ansible_python_interpreter': sys.executable,
                    'ansible_remote_tmp': str(temp / 'remote'),
                    'infrabox_netbox_id': 1, 'infrabox_netbox_virtual': True}}},
                'test_bad': {'hosts': {'beta': {'ansible_connection': 'does_not_exist',
                    'infrabox_netbox_id': 2, 'infrabox_netbox_virtual': False}}}}}}))
            with contextlib.redirect_stdout(io.StringIO()):
                rc = run(temp / 'subset', ROOT, 'test_*:!test_bad', REVISION, inventory=inventory, timeout=60)
            manifest = json.loads((temp / 'subset/run.json').read_text())
            self.assertEqual(rc, 0, manifest)
            self.assertEqual([h['name'] for h in manifest['hosts']], ['alpha'])
            facts = json.loads((temp / 'subset/facts/vm-1.json').read_text())
            self.assertTrue(facts)
            self.assertNotIn('ansible_env', facts)
            self.assertNotIn('ansible_local', facts)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = run(temp / 'none', ROOT, 'missing*', REVISION, inventory=inventory, timeout=60)
            self.assertEqual(rc, 1)
            self.assertEqual(json.loads((temp / 'none/run.json').read_text())['outcome'], 'no_targets')
            with contextlib.redirect_stdout(io.StringIO()):
                rc = run(temp / 'partial', ROOT, 'all', REVISION, inventory=inventory, timeout=60)
            self.assertEqual(rc, 1)
            manifest = json.loads((temp / 'partial/run.json').read_text())
            self.assertEqual(manifest['outcome'], 'partial_failure', manifest)
            self.assertTrue((temp / 'partial/facts/vm-1.json').exists())


if __name__ == '__main__':
    unittest.main()
