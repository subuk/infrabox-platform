import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from reconcile_netbox import normalize, Reconciler, reconcile

FACTS = {'ansible_distribution': 'AlmaLinux', 'ansible_distribution_version': '10.2',
         'ansible_architecture': 'x86_64', 'ansible_processor_vcpus': 4, 'ansible_memtotal_mb': 7800}

class Object(NS):
    def update(self, values):
        self.writes.append(values)
        self.__dict__.update(values)

class Endpoint:
    def __init__(self, objects=()): self.objects = list(objects)
    def get(self, pk=None, **query):
        return next((x for x in self.objects if x.id == pk), None) if pk else next(iter(self.filter(**query)), None)
    def filter(self, **query):
        return [x for x in self.objects if all(k == 'tag' or getattr(x, k, None) == v for k, v in query.items())]
    def create(self, values): raise AssertionError('Unexpected create')

def fixture():
    host = Object(id=1, name='vm', tags=[NS(slug='infrabox-managed')], platform=NS(id=2), serial='',
                  vcpus=4, memory=7800, custom_fields={'discovery_architecture':'x86_64','operator':'keep'},
                  role='server', site='home', writes=[])
    api = NS(dcim=NS(devices=Endpoint(), platforms=Endpoint([NS(id=2,name='AlmaLinux 10.2',manufacturer=None)])),
             virtualization=NS(virtual_machines=Endpoint([host])))
    return host, api

class ReconciliationTests(unittest.TestCase):
    def test_representative_normalization_and_dmi(self):
        h=normalize(FACTS,'vm')
        self.assertEqual((h.platform,h.vcpus,h.memory,h.architecture),('AlmaLinux 10.2',4,7800,'x86_64'))
        h=normalize(FACTS,'device',{'status':'succeeded','stdout':'Handle 0x01, DMI type 17, 84 bytes\nMemory Device\n\tLocator: DIMM_A1\n\tManufacturer: Samsung\n\tPart Number: ABC\n\tSize: 16 GiB\n\tSerial Number: 123\n\tType: DDR4\n'})
        self.assertEqual(h.components[0].attributes,{'capacity_mib':16384,'technology':'DDR4'})
        self.assertIn('dmi_unavailable',normalize(FACTS,'device',{'status':'unavailable'}).warnings)
        names=['eno1','bond0','br0','vlan100','cilium_host','cilium_net','lxc_health','lxc123abc']
        facts={**FACTS,'ansible_interfaces':names,**{'ansible_'+n:{} for n in names}}
        with patch.dict('os.environ', {'PLATFORM_INTERFACE_EXCLUDE_PATTERNS':'cilium_*,lxc*'}):
            self.assertEqual([i.name for i in normalize(facts,'vm').interfaces],names[:4])
        with patch.dict('os.environ', {'PLATFORM_INTERFACE_EXCLUDE_PATTERNS':''}):
            self.assertEqual([i.name for i in normalize(facts,'vm').interfaces],names)

    def test_unchanged_has_no_writes(self):
        host,api=fixture()
        r=Reconciler(api)
        observation={'discovery_last_success':'2026-09-18T00:00:00+00:00','discovery_source':'ansible'}
        host.custom_fields.update(observation)
        host.custom_fields['discovery_last_success']='2026-09-18T00:00:00Z'
        r.apply({'object_id':1,'object_type':'vm','name':'vm'},normalize(FACTS,'vm'),observation)
        self.assertEqual(host.writes,[])

    def test_minimal_update_preserves_user_fields(self):
        host,api=fixture();r=Reconciler(api)
        r.apply({'object_id':1,'object_type':'vm','name':'vm'},normalize({**FACTS,'ansible_processor_vcpus':8},'vm'),{})
        self.assertEqual(host.writes,[{'vcpus':8}])
        self.assertEqual((host.role,host.site,host.custom_fields['operator']),('server','home','keep'))

    def test_ambiguous_platform_is_not_applied(self):
        host,api=fixture();api.dcim.platforms.objects.append(NS(id=3,name='AlmaLinux 10.2',manufacturer=None))
        r=Reconciler(api);r.apply({'object_id':1,'object_type':'vm','name':'vm'},normalize(FACTS,'vm'),{})
        self.assertEqual(host.writes,[]);self.assertIn('platform_ambiguous',r.warnings)

    def test_failure_keeps_other_host_success(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);(path/'facts').mkdir()
            records=[{'object_id':i,'object_type':'vm','name':str(i),'status':'succeeded'} for i in (1,2)]
            for i in (1,2): (path/'facts'/f'vm-{i}.json').write_text(json.dumps(FACTS))
            (path/'run.json').write_text(json.dumps(dict(run_id='1',attempt='1',revision='a'*40,pattern='1,2',started_at='2026-09-18T00:00:00Z',finished_at='2026-09-18T00:01:00Z',ansible_return_code=0,outcome='success',hosts=records)))
            error=RuntimeError('private body');error.req=NS(status_code=401)
            with patch.object(Reconciler,'apply',side_effect=[error,False]):
                report=reconcile(path,None)
            self.assertEqual(report['counts']['reconciled'],1)
            self.assertEqual(report['counts']['failed'],1)
            self.assertIn('netbox_auth',report['hosts'][0]['errors'])
            self.assertNotIn('private body',json.dumps(report))

    def test_disk_normalization_identity_capacity_and_exclusions(self):
        base={'sectors':1000000,'sectorsize':'4096','model':'Example SSD','vendor':'Example',
              'serial':'SER1','wwn':'0x5000123456789abc','rotational':'0'}
        devices={name:dict(base) for name in ('sda','sdb','sda1','dm-0','md0','loop0','zram0','sr0')}
        facts={**FACTS,'ansible_devices':devices}
        vm=normalize(facts,'vm')
        self.assertEqual([(d.name,d.size) for d in vm.disks],[('sda',4096)])
        physical=normalize(facts,'device')
        self.assertEqual(len(physical.components),1)
        disk=physical.components[0]
        self.assertEqual((disk.profile,disk.serial,disk.identity),('Disk','SER1','wwn:5000123456789abc'))
        self.assertEqual(disk.attributes,{'capacity_bytes':4096000000,'rotational':False})
        self.assertEqual(normalize({**facts,'ansible_virtualization_role':'guest'},'device').components,[])
        self.assertEqual(normalize({**FACTS,'ansible_devices':{'sda':{'sectors':1}}},'device').components,[])
        nvme={**base,'wwn':None,'serial':None,'vendor':None,'links':{'ids':['nvme-eui.353041304e2850330025384600000001']}}
        d=normalize({**FACTS,'ansible_devices':{'nvme0n1':nvme}},'device').components[0]
        self.assertEqual(d.manufacturer,'InfraBox Generic')
        self.assertTrue(d.identity.startswith('wwn:3530'))
        self.assertEqual(d.serial,'')
        devices['sdb']['sectors']=999
        ambiguous=normalize(facts,'device')
        self.assertEqual(ambiguous.components,[])
        self.assertIn('disk_identity_ambiguous:sdb',ambiguous.warnings)

    def test_virtual_disk_create_resize_preserve_and_replay(self):
        class Disks(Endpoint):
            def create(self, values):
                obj=Object(id=9,virtual_machine_id=values['virtual_machine'],writes=[],owner='keep',description='keep',**values)
                self.objects.append(obj)
                return obj
        host,api=fixture();api.virtualization.virtual_disks=Disks()
        facts={**FACTS,'ansible_devices':{'vda':{'sectors':20971520,'sectorsize':512}}}
        record={'object_id':1,'object_type':'vm','name':'vm'}
        r=Reconciler(api);r.apply(record,normalize(facts,'vm'),{})
        disk=api.virtualization.virtual_disks.objects[0]
        self.assertEqual((disk.name,disk.size),('vda',10738))
        self.assertTrue(r.changes[0]['created'])
        r=Reconciler(api);r.apply(record,normalize(facts,'vm'),{})
        self.assertEqual(r.changes,[])
        facts['ansible_devices']['vda']['sectors']*=2
        Reconciler(api).apply(record,normalize(facts,'vm'),{})
        self.assertEqual(disk.writes,[{'size':21475}])
        self.assertEqual((disk.owner,disk.description),('keep','keep'))
        r=Reconciler(api);r.apply(record,normalize(FACTS,'vm'),{})
        self.assertEqual(r.changes,[])
        self.assertEqual(len(api.virtualization.virtual_disks.objects),1)

    def test_workflow_separates_compact_and_debug_artifacts(self):
        text=(Path(__file__).resolve().parents[1]/'.gitea/workflows/discover.yml').read_text()
        self.assertIn('scripts/reconcile_netbox.py "$PLATFORM_RESULTS_DIR"',text)
        compact=text.split('name: Publish compact reconciliation result')[1].split('- name: Publish operator')[0]
        self.assertIn('reconciliation-summary.json',compact)
        self.assertNotIn('facts/*.json',compact)
        self.assertIn('facts/*.json',text)

if __name__=='__main__': unittest.main()
