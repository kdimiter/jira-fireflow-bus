"""To Do intake follows FireFlow approval rather than a local approval ledger."""
import copy
import unittest
from tests import test_approval_runtime as fixtures
from algosec_jira_bus import doctor
from algosec_jira_bus.sync import MappingError


class TodoIntake(unittest.TestCase):
    intake = fixtures.ApprovalRuntime.intake
    assert_refused_without_create = fixtures.ApprovalRuntime.assert_refused_without_create
    # Reuse fixture helpers, not inherited approval-mode tests.
    def setUp(self):
        fixtures.ApprovalRuntime.setUp(self)
        self.settings['intake'] = {'mode': 'todo'}
        self.jira.fresh['fields']['status']['name'] = 'To Do'
        self.jira.search_snapshot = copy.deepcopy(self.jira.fresh)

    def test_todo_submits_fresh_payload_once_without_capture(self):
        self.jira.fresh['fields']['customfield_1']['trafficLines'][0]['destination']['value'] = '192.0.2.2'
        result = self.intake()
        self.assertEqual(len(result['created']), 1)
        self.assertEqual(self.fireflow.calls[0][0]['traffic'][0]['destination']['items'], [{'address': '192.0.2.2'}])
        self.assertEqual(self.intake()['skipped'], ['NET-1'])
        self.assertEqual(len(self.fireflow.calls), 1)
        self.assertFalse(list(self.ledger.directory.glob('*.json')))

    def test_status_changed_since_search_refuses(self):
        self.jira.fresh['fields']['status']['name'] = 'Approved'
        self.assert_refused_without_create()

    def test_different_immutable_id_refuses(self):
        self.jira.fresh['id'] = '9999'
        self.assert_refused_without_create()

    def test_missing_immutable_id_refuses(self):
        self.jira.search_snapshot.pop('id')
        self.assert_refused_without_create()

    def test_unknown_mode_refuses_configuration(self):
        self.settings['intake']['mode'] = 'automatic'
        with self.assertRaises(MappingError):
            self.intake()
        self.assertEqual(self.fireflow.calls, [])
        self.assertTrue(any(n['check'] == 'intake' and n['level'] == doctor.FAIL
                            for n in doctor.local(self.settings, self.state)))

    def test_todo_requires_structured_mapping(self):
        self.settings['mapping'] = {}
        with self.assertRaises(MappingError):
            self.intake()
