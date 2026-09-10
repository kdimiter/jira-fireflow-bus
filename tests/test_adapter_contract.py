"""Dry run must validate against the installed adapter, not just our own builder."""
import tempfile
import unittest
from pathlib import Path
from algosec_jira_bus.sync import run, State
from test_sync import MAPPING, Jira, Fireflow, issue
from test_traffic_lines import TABLE_MAPPING, table, document, issue as table_issue, paragraph


class AdapterContract(unittest.TestCase):
    def check_refused(self, issue_value, mapping, message='adapter'):
        with tempfile.TemporaryDirectory() as d:
            state = State(Path(d) / 'state.json')
            f = Fireflow()
            settings = {'jira': {'jql': 'project = NET'}, 'mapping': mapping,
                        'fireflow': {'template': 'T', 'devices': ['fw1']}}
            result = run(settings, f, state, Jira([issue_value]), dry_run=True, log=lambda *a: None)
            self.assertEqual(result['created'], [])
            self.assertEqual(len(result['refused']), 1)
            self.assertIn(message, result['refused'][0][1].lower())
            self.assertEqual(f.calls, [])

    def test_named_objects_are_accepted_by_the_installed_adapter(self):
        with tempfile.TemporaryDirectory() as d:
            state = State(Path(d) / 'state.json')
            fireflow = Fireflow()
            settings = {'jira': {'jql': 'project = NET'}, 'mapping': MAPPING,
                        'fireflow': {'template': 'T', 'devices': ['fw1']}}
            result = run(settings, fireflow, state, Jira([issue(source='srv-app-01')]),
                         dry_run=True, log=lambda *a: None)
            self.assertEqual(len(result['created']), 1)
            self.assertEqual(result['refused'], [])
            self.assertEqual(result['created'][0][1]['traffic'][0]['source']['items'],
                             [{'name': 'srv-app-01'}])
            self.assertEqual(fireflow.calls, [])

    def test_101_lines_are_refused_by_current_adapter_before_apply(self):
        grid = [['Source', 'Destination', 'Service']] + [['192.0.2.1','198.51.100.1','tcp/443']] * 101
        self.check_refused(table_issue(document(table(grid))), TABLE_MAPPING, 'at most 100')

    def test_multiline_cells_keep_addresses_separate(self):
        from algosec_jira_bus.sync import build
        t = table([['Source', 'Destination', 'Service'], ['unused','198.51.100.1','tcp/443']])
        t['content'][1]['content'][0]['content'] = [paragraph('192.0.2.1'), paragraph('192.0.2.2')]
        _, request = build(table_issue(document(t)), TABLE_MAPPING, 'T', ['fw1'])
        self.assertEqual(request['traffic'][0]['source']['items'],
                         [{'address':'192.0.2.1'}, {'address':'192.0.2.2'}])
