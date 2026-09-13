"""Exercise the actual pinned NetBox inventory plugin with fixture API responses."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from ansible.plugins.loader import init_plugin_loader
ROOT = Path(__file__).resolve().parents[1]
init_plugin_loader([str(ROOT / '.ansible/collections')])
from ansible_collections.netbox.netbox.plugins.inventory.nb_inventory import InventoryModule
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader


class NativeInventoryTests(unittest.TestCase):
    def test_flattened_context_native_address_and_patterns(self):
        calls = []
        def fetch(plugin, url):
            calls.append(url)
            path = urlsplit(url).path
            if path == '/api/status/':
                return {'netbox-version': '4.7.0'}
            if path == '/api/schema/':
                return {'info': {'version': '4.7.0'}, 'paths': {
                    p: {'get': {'parameters': [{'name': 'tag'}]}}
                    for p in ['/api/dcim/devices/', '/api/virtualization/virtual-machines/']}}
            records = []
            if path == '/api/dcim/devices/':
                self.assertEqual(parse_qs(urlsplit(url).query)['tag'], ['infrabox-managed'])
                records = [{'id': 7, 'name': 'router1', 'platform': None, 'tags': [],
                            'config_context': {'ansible_connection': 'ansible.netcommon.network_cli',
                                               'ansible_network_os': 'vendor.os', 'ansible_port': 2207,
                                               'operator_arbitrary_var': {'nested': 42}},
                            'primary_ip4': {'address': '192.0.2.7/24'}}]
            if path == '/api/virtualization/virtual-machines/':
                self.assertEqual(parse_qs(urlsplit(url).query)['tag'], ['infrabox-managed'])
                records = [{'id': 8, 'name': 'vm1', 'platform': None, 'tags': [],
                            'config_context': {'ansible_user': 'operator', 'ansible_host': '2001:db8::8'}}]
            return {'results': records, 'next': None, 'count': len(records)}

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'inventory.netbox.yml'
            path.write_text((ROOT / 'inventory/netbox.yml').read_text())
            env = {'NETBOX_API': 'https://netbox.example.com', 'NETBOX_TOKEN': 'fixture-only',
                   'SSL_CERT_FILE': '/dev/null', 'PLATFORM_MANAGED_TAG': 'infrabox-managed'}
            with patch.dict(os.environ, env), patch.object(InventoryModule, '_fetch_information', fetch):
                loader = DataLoader()
                inventory = InventoryManager(loader, sources=[str(path)])
            one, two = inventory.get_host('router1'), inventory.get_host('vm1')
            self.assertIsNotNone(one)
            self.assertIsNotNone(two)
            self.assertEqual(one.vars['ansible_port'], 2207)
            self.assertEqual(one.vars['operator_arbitrary_var'], {'nested': 42})
            self.assertEqual(one.vars['ansible_connection'], 'ansible.netcommon.network_cli')
            self.assertEqual(one.vars['ansible_host'], '192.0.2.7')
            self.assertEqual(two.vars['ansible_host'], '2001:db8::8')
            self.assertNotIn('ansible_port', two.vars)
            self.assertEqual(one.vars['infrabox_netbox_id'], 7)
            self.assertFalse(one.vars['infrabox_netbox_virtual'])
            self.assertTrue(two.vars['infrabox_netbox_virtual'])
            inventory.subset('all:!router*')
            self.assertEqual([h.name for h in inventory.get_hosts('all')], ['vm1'])
            self.assertTrue(calls)
