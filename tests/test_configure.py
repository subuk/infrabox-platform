import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml
from jsonschema import Draft202012Validator
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from configure import boolean, policy, registry, validate_hosts, write_inventory_snapshot, parse_inventory_output
from impact import PolicyError, impact, changed_paths
from runtime_contract import fingerprint

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.schema=json.loads((ROOT/'schemas/config-context.schema.json').read_text())
        self.hosts={'one':{'infrabox_netbox_id':1,'infrabox_netbox_virtual':False,
                         'infrabox_roles':{'chrony':{'enabled':True}},'chrony_servers':['ntp.example.com']},
                    'two':{'infrabox_netbox_id':2,'infrabox_netbox_virtual':True,
                         'infrabox_roles':{'sshd':{'enabled':True}}}}
    def test_inventory_warning_does_not_corrupt_json_or_hide_failures(self):
        warning='[WARNING]: Invalid characters were found in group names but not replaced, use -vvvv to see details\n'
        self.assertEqual(parse_inventory_output(0,'{"_meta":{"hostvars":{}}}',warning),{'_meta':{'hostvars':{}}})
        for code,text,error in ((1,'{}',''),(0,'{}','HTTP Error 403'),(0,'{}',warning+'API lookup failed'),(0,'bad','')):
            with self.assertRaises(PolicyError):parse_inventory_output(code,text,error)

    def test_role_union_and_disabled(self):
        self.hosts['two']['infrabox_roles']['chrony']={'enabled':False}
        self.assertEqual(impact(['roles/chrony/tasks/main.yml'],self.hosts)['hosts'],['one'])
        self.assertEqual(impact(['roles/chrony/x','roles/sshd/x'],self.hosts)['hosts'],['one','two'])
    def test_every_changed_role_requires_target_even_with_global_paths(self):
        for paths in (['roles/chrony/x','roles/packages/x'],['site.yml','roles/packages/x']):
            with self.assertRaisesRegex(PolicyError,'packages'): impact(paths,self.hosts)
    def test_conservative_paths(self):
        for path in ('site.yml','inventory/netbox.yml','schemas/x','scripts/x','requirements.yml','new-unknown-file','.gitea/workflows/configure.yml','roles/chrony'):
            self.assertEqual(impact([path],self.hosts)['scope'],'all')
        self.assertEqual(impact(['README.md','docs/howto.md'],self.hosts)['scope'],'none')
    def test_schema_accepts_partial_and_native_contexts(self):
        v=Draft202012Validator(self.schema)
        for data in ({'chrony_makestep':False},{'infrabox_roles':{'chrony':{'enabled':False}}},
                     {'ansible_user':'operator','site_metadata':{'x':1}}):
            v.validate(data)
    def test_typos_types_and_runtime_inputs_rejected(self):
        for update in ({'chrony_severs':[]},{'chrony_makestep':'true'},
                       {'chrony_runtime':{}},{'infrabox_runtime_role':'sshd'},
                       {'infrabox_roles':{'typo':{'enabled':False}}}):
            hosts=copy.deepcopy(self.hosts);hosts['one'].update(update)
            with self.assertRaises(PolicyError): validate_hosts(hosts,registry(ROOT),self.schema)
    def test_host_identity_and_limit_injection_rejected(self):
        for name in ('a,b','all','a:*','@file','-x'):
            with self.assertRaises(PolicyError): validate_hosts({name:self.hosts['one']},registry(ROOT),self.schema)
        hosts=copy.deepcopy(self.hosts);hosts['two'].update(infrabox_netbox_id=1,infrabox_netbox_virtual=False)
        with self.assertRaises(PolicyError): validate_hosts(hosts,registry(ROOT),self.schema)
    def test_registry_and_defaults_match_schema(self):
        order=registry(ROOT);self.assertEqual(order,['packages','chrony','sshd'])
        for role in order:
            defaults=yaml.safe_load((ROOT/'roles'/role/'defaults/main.yml').read_text())
            self.assertTrue(all(k.startswith(role+'_') for k in defaults))
            self.assertTrue(set(defaults)<=set(self.schema['properties']))
        validate_hosts(self.hosts,order,self.schema)
    def test_inventory_marked_strings_keep_types_and_remain_untrusted(self):
        from ansible._internal._json._profiles import _inventory_legacy
        from ansible._internal._datatag import _tags
        from ansible.parsing.dataloader import DataLoader
        from ansible.inventory.manager import InventoryManager
        from ansible.plugins.loader import init_plugin_loader
        init_plugin_loader()
        data={'_meta':{'hostvars':{'one':{**self.hosts['one'], 'packages_names':['curl'], 'external_literal':'{{ 7 * 7 }}'}}},'all':{'hosts':['one']}}
        encoded=json.dumps(data,cls=_inventory_legacy.Encoder)
        self.assertIn('__ansible_unsafe',encoded)
        decoded=parse_inventory_output(0,encoded,'')
        validate_hosts(decoded['_meta']['hostvars'],registry(ROOT),self.schema)
        self.assertEqual(decoded['_meta']['hostvars']['one']['packages_names'],['curl'])
        with tempfile.TemporaryDirectory() as temp:
            snapshot=write_inventory_snapshot(encoded,temp)
            inventory=InventoryManager(loader=DataLoader(),sources=[str(snapshot)])
            literal=inventory.get_host('one').vars['external_literal']
            self.assertEqual(literal,'{{ 7 * 7 }}')
            self.assertFalse(_tags.TrustedAsTemplate.is_tagged_on(literal))

    def test_native_snapshot_preserves_hosts_groups_and_vars(self):
        from ansible.parsing.dataloader import DataLoader
        from ansible.inventory.manager import InventoryManager
        data={'_meta':{'hostvars':self.hosts},'all':{'children':['site_lab']},'site_lab':{'hosts':['one','two'],'vars':{'site_var':True}}}
        with tempfile.TemporaryDirectory() as temp:
            from ansible._internal._json._profiles import _inventory_legacy
            p=write_inventory_snapshot(json.dumps(data,cls=_inventory_legacy.Encoder),temp)
            inventory=InventoryManager(loader=DataLoader(),sources=[str(p)])
            self.assertEqual([h.name for h in inventory.get_hosts('site_lab:!two')],['one'])
            self.assertEqual(inventory.get_host('one').vars['chrony_servers'],['ntp.example.com'])
    def test_pr_cannot_override_mode_and_requires_exact_same_repo_head(self):
        env={'GITEA_REPOSITORY':'org/repo','PLATFORM_CHECK':'false'}
        event={'inputs':{'check':'false'},'pull_request':{'head':{'sha':'b'*40,'repo':{'full_name':'org/repo'}},'base':{'sha':'a'*40,'repo':{'full_name':'org/repo'}}}}
        p=policy('pull_request',event,env,'b'*40,'a'*40)
        self.assertTrue(p['check']);self.assertTrue(p['diff'])
        with self.assertRaises(PolicyError):policy('pull_request',event,env,'a'*40,'a'*40)
        event['pull_request']['head']['repo']['full_name']='fork/repo'
        with self.assertRaises(PolicyError):policy('pull_request',event,env,'b'*40,'a'*40)
    def test_manual_defaults_explicit_apply_and_revision_gate(self):
        p=policy('workflow_dispatch',{}, {},'a','a')
        self.assertEqual((p['check'],p['diff'],p['limit']),(True,True,'all'))
        self.assertFalse(policy('workflow_dispatch',{'inputs':{'check':'false'}},{},'a','a')['check'])
        with self.assertRaises(PolicyError):policy('workflow_dispatch',{}, {},'b','a')
        with self.assertRaises(PolicyError):policy('push',{}, {},'a','a')
        for value in ('yes','0','False',0,1,[],{}):
            with self.assertRaises(PolicyError):boolean(value,True)
    def test_git_move_is_both_roles_and_runtime_has_no_commit_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            def git(*args):return subprocess.check_output(['git','-C',temp,*args],text=True).strip()
            git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.com')
            p=Path(temp)/'roles/chrony/tasks';p.mkdir(parents=True);(p/'x').write_text('same')
            git('add','.');git('commit','-qm','base');base=git('rev-parse','HEAD')
            dest=Path(temp)/'roles/sshd/tasks';dest.mkdir(parents=True);(p/'x').rename(dest/'x')
            git('add','.');git('commit','-qm','move');head=git('rev-parse','HEAD')
            self.assertEqual(set(changed_paths(temp,base,head)),{'roles/chrony/tasks/x','roles/sshd/tasks/x'})
        self.assertEqual(len(fingerprint(ROOT)),64)

if __name__=='__main__':unittest.main()

class PreflightBarrierTests(unittest.TestCase):
    def test_failure_on_one_host_prevents_changes_on_all_hosts(self):
        # Disposable local fixtures only: validates orchestration, not PR target coverage.
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'site.yml').write_text((ROOT/'site.yml').read_text())
            (root/'role-order.json').write_text('["probe"]')
            for sub in ('tasks','defaults'):(root/'roles/probe'/sub).mkdir(parents=True)
            (root/'roles/probe/defaults/main.yml').write_text('{}')
            (root/'roles/probe/tasks/validate.yml').write_text('- ansible.builtin.assert:\n    that: inventory_hostname != "bad"\n')
            marker=root/'must-not-exist'
            (root/'roles/probe/tasks/main.yml').write_text('- ansible.builtin.copy:\n    content: mutation\n    dest: '+str(marker)+'\n')
            hosts={name:{'ansible_connection':'local','ansible_python_interpreter':sys.executable,'ansible_remote_tmp':str(root/'remote-tmp'),'infrabox_roles':{'probe':{'enabled':True}}} for name in ('good','bad')}
            (root/'inventory.yml').write_text(yaml.safe_dump({'all':{'hosts':hosts}}))
            (root/'ansible.cfg').write_text('[defaults]\n')
            env=dict(os.environ,ANSIBLE_CONFIG=str(root/'ansible.cfg'),ANSIBLE_STDOUT_CALLBACK='default',ANSIBLE_LOCAL_TEMP=str(root/'tmp'))
            result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(root/'inventory.yml'),str(root/'site.yml')],cwd=root,env=env,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0,result.stdout)
            self.assertIn('fatal: [bad]: FAILED!',result.stdout)
            self.assertIn('All assertions passed',result.stdout)
            self.assertFalse(marker.exists(),'A host mutated before global preflight passed')
