"""Exercise the real approval ledger at the intake-to-FireFlow boundary."""
import copy
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.approval_gate import ApprovalLedger
from algosec_jira_bus.sync import State, run


class Jira:
    def __init__(self, origin='https://example.atlassian.net'):
        self.config = {'base_url': origin}
        self.fresh = {'id': '1234', 'key': 'NET-1', 'fields': {
            'summary': 'Approved access', 'status': {'name': 'Approved'},
            'creator': {'displayName': 'Ticket Creator',
                        'emailAddress': 'creator@example.org'},
            'customfield_1': {'schemaVersion': 1, 'justification': 'Business need',
                'changeType': 'Allow', 'trafficLines': [{
                    'source': {'kind': 'subnet', 'value': '203.0.113.0/24'},
                    'destination': {'kind': 'ip', 'value': '192.0.2.1'},
                    'services': [{'kind': 'port', 'protocol': 'tcp', 'port': 443}]}]}}}
        self.search_snapshot = copy.deepcopy(self.fresh)
        self.reads = []

    def search(self, jql, fields, limit=50):
        return [copy.deepcopy(self.search_snapshot)]

    def read_issue(self, identifier, fields):
        self.reads.append(identifier)
        snapshot = copy.deepcopy(self.fresh)
        snapshot['fields'] = {
            name: value for name, value in snapshot['fields'].items()
            if name in fields
        }
        return snapshot


class Fireflow:
    def __init__(self):
        self.calls = []

    def create(self, request, operation_id, reason):
        self.calls.append((copy.deepcopy(request), operation_id))
        return {'operation_id': operation_id, 'receipt': 'receipt', 'response': {'id': 42}}


class ApprovalRuntime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = State(Path(self.temp.name) / 'state.json')
        self.jira, self.fireflow = Jira(), Fireflow()
        self.settings = {'jira': {**self.jira.config, 'jql': 'project = NET'},
                         'mapping': {'structured': {'field': 'customfield_1'}},
                         'fireflow': {'template': 'Шаблон', 'devices': ['fw1']}}
        self.ledger = ApprovalLedger(self.state.path.parent / 'approvals')

    def capture(self):
        self.ledger.capture(self.jira, '1234', 'customfield_1', 'Шаблон', ['fw1'],
                            operator='pilot-admin')

    def intake(self, state=None):
        return run(self.settings, self.fireflow, state or self.state, self.jira,
                   dry_run=False, log=lambda *args: None)

    def assert_refused_without_create(self):
        result = self.intake()
        self.assertEqual([key for key, _ in result['refused']], ['NET-1'])
        self.assertEqual(self.fireflow.calls, [])
        self.assertFalse(self.state.seen('NET-1'))

    def test_failed_create_preserves_intent_without_marking_seen_and_can_retry_unsent(self):
        from algosec_jira_bus.queue import Failures
        self.capture()
        self.fireflow.config = {'base_url': 'https://asms.example.test'}
        self.fireflow.state = Path(self.temp.name) / 'receipts'
        self.fireflow.state.mkdir()
        original = self.fireflow.create
        def fail(request, operation, reason):
            persisted = State(self.state.path).data['intents']['NET-1']
            self.assertEqual(persisted, {'operation_id': operation, 'jira_issue_id': '1234'})
            raise ValueError('preflight refused')
        self.fireflow.create = fail
        self.assertTrue(self.intake()['failed'])
        self.assertFalse(self.state.seen('NET-1'))
        self.assertFalse(Failures(self.state).entry('NET-1')['parked'])
        Failures(self.state).clear('NET-1')
        self.fireflow.create = original
        self.assertTrue(self.intake()['created'])

    def test_missing_approval_does_not_call_fireflow(self):
        self.assert_refused_without_create()
        self.assertEqual(self.jira.reads, ['1234'])

    def test_changed_approved_payload_does_not_call_fireflow(self):
        self.capture()
        self.jira.fresh['fields']['customfield_1']['trafficLines'][0]['destination']['value'] = '192.0.2.2'
        self.assert_refused_without_create()

    def test_revoked_status_does_not_call_fireflow(self):
        self.capture()
        self.jira.fresh['fields']['status']['name'] = 'To Do'
        self.assert_refused_without_create()

    def test_maps_fresh_approved_snapshot_instead_of_search_contents(self):
        self.jira.fresh['fields']['customfield_1']['trafficLines'][0]['destination']['value'] = '192.0.2.2'
        self.jira.fresh['fields']['summary'] = 'Fresh summary'
        self.capture()
        result = self.intake()
        self.assertEqual(len(result['created']), 1)
        request = self.fireflow.calls[0][0]
        self.assertEqual(request['traffic'][0]['destination']['items'], [{'address': '192.0.2.2'}])
        fields = {field['name']: field['values'] for field in request['fields']}
        self.assertEqual(fields['subject'], ['NET-1: Fresh summary'])
        self.assertEqual(fields['Requestor'], ['creator@example.org'])
        self.assertEqual(self.jira.reads, ['1234', '1234'])

    def test_key_rename_keeps_operation_identity_even_without_state(self):
        self.capture()
        self.intake()
        original_operation = self.fireflow.calls[-1][1]
        self.jira.fresh['key'] = 'MOVED-2'
        self.jira.search_snapshot = copy.deepcopy(self.jira.fresh)
        self.intake(State(self.state.path.parent / 'empty-state.json'))
        self.assertEqual(self.fireflow.calls[-1][1], original_operation)
        self.assertTrue(original_operation.endswith('-1234'))
        fields = {field['name']: field['values'] for field in self.fireflow.calls[-1][0]['fields']}
        self.assertTrue(fields['subject'][0].startswith('MOVED-2:'))
        self.assertNotIn('externalId', fields)

    def test_recorded_immutable_id_skips_renamed_issue_after_restart(self):
        self.capture()
        self.intake()
        self.assertEqual(self.state.entries()['NET-1']['jira_issue_id'], '1234')
        self.jira.fresh['key'] = 'MOVED-2'
        self.jira.search_snapshot = copy.deepcopy(self.jira.fresh)
        result = self.intake(State(self.state.path))
        self.assertEqual(result['skipped'], ['MOVED-2'])
        self.assertEqual(len(self.fireflow.calls), 1)

    def test_different_tenant_does_not_share_operation_identity(self):
        self.capture()
        self.intake()
        original_operation = self.fireflow.calls[-1][1]
        self.jira.config['base_url'] = 'https://other.atlassian.net'
        self.settings['jira']['base_url'] = self.jira.config['base_url']
        self.capture()
        self.intake(State(self.state.path.parent / 'other-state.json'))
        self.assertNotEqual(self.fireflow.calls[-1][1], original_operation)
