import unittest
from algosec_jira_bus.structured import address, normalize

class Structured(unittest.TestCase):
    def test_range_stays_one_value(self):
        self.assertEqual(address({'kind': 'range', 'value': '192.0.2.1–192.0.2.9'})['value'], '192.0.2.1-192.0.2.9')
    def test_reversed_range_rejected(self):
        with self.assertRaises(ValueError): address({'kind': 'range', 'value': '192.0.2.9-192.0.2.1'})
    def test_subnet_stays_one_value(self):
        self.assertEqual(address({'kind': 'subnet', 'value': '203.0.113.0/24'})['value'], '203.0.113.0/24')
    def test_invalid_ip_rejected(self):
        with self.assertRaises(ValueError): address({'kind': 'ip', 'value': '999.1.1.1'})
    def test_hostname_preserves_type(self):
        self.assertEqual(address({'kind': 'hostname', 'value': 'app.example.local'})['kind'], 'hostname')
    def test_boolean_version_rejected(self):
        with self.assertRaises(ValueError): normalize({'schemaVersion': True})
    def test_structured_mapping_does_not_fall_back_to_description(self):
        from algosec_jira_bus.sync import build, MappingError
        with self.assertRaises(MappingError):
            build({'key': 'NET-1', 'fields': {}}, {'structured': {'field': 'customfield_1'}, 'table': {}}, 'T', ['D'])

    def test_multiple_rows_remain_in_one_request(self):
        from algosec_jira_bus.sync import build
        row = {'source': {'kind': 'subnet', 'value': '203.0.113.0/24'},
               'destination': {'kind': 'ip', 'value': '192.0.2.1'},
               'services': [{'kind': 'port', 'protocol': 'tcp', 'port': 443}]}
        raw = {'schemaVersion': 1, 'justification': 'Business need',
               'changeType': 'Allow', 'trafficLines': [row, row]}
        key, request = build({'key': 'NET-1', 'fields': {
                                 'customfield_1': raw,
                                 'creator': {'displayName': 'Ticket Creator',
                                             'emailAddress': 'creator@example.org'}}},
                             {'structured': {'field': 'customfield_1'}}, 'T', ['D'])
        self.assertEqual(len(request['traffic']), 2)
        self.assertEqual(request['traffic'][0]['source']['items'], [{'address': '203.0.113.0/24'}])
        self.assertIn({'name': 'Change Request Description', 'values': ['Business need']}, request['fields'])

    def test_hostname_uses_fireflow_named_traffic_item(self):
        from algosec_jira_bus.sync import build
        raw = {
            'schemaVersion': 1,
            'justification': 'Business need',
            'changeType': 'Allow',
            'trafficLines': [{
                'source': {'kind': 'ip', 'value': '192.0.2.10'},
                'destination': {'kind': 'hostname', 'value': 'app.example.local'},
                'services': [{'kind': 'port', 'protocol': 'tcp', 'port': 443}],
            }],
        }

        _key, request = build(
            {'key': 'NET-1', 'fields': {'customfield_1': raw}},
            {'structured': {'field': 'customfield_1'}}, 'Basic', ['device'])

        line = request['traffic'][0]
        self.assertEqual(line['source']['items'], [{'address': '192.0.2.10'}])
        self.assertEqual(line['destination']['items'],
                         [{'name': 'app.example.local'}])
