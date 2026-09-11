import tempfile
import unittest
from pathlib import Path
from algosec_jira_bus.sync import State, mirror

class ResultDelivery(unittest.TestCase):
    def test_owner_mapping_updates_the_jira_assignee_with_result_fields(self):
        with tempfile.TemporaryDirectory() as root:
            state = State(Path(root)/'state.json')
            state.record('NET-1', {'change_request_id': 49, 'status': None})
            writes = []
            class Jira:
                def update_fields(self, key, fields): writes.append((key, fields))
                def comment(self, key, text): pass
            class FF:
                def get(self, identifier):
                    return {'response': {'id': identifier, 'status': 'plan',
                                         'fields': [{'name': 'Owner',
                                                     'values': ['Alice.Admin']}]}}
            settings = {'jira': {}, 'mirror': {
                'result_fields': {'owner': 'customfield_123'},
                'owner_assignees': {'alice.admin': '712020:abc-123'}}}
            mirror(settings, FF(), state, Jira(), dry_run=False, log=lambda *a: None)
            self.assertEqual(writes, [('NET-1', {
                'customfield_123': 'Alice.Admin',
                'assignee': {'accountId': '712020:abc-123'}})])

    def test_unmapped_owner_does_not_change_the_jira_assignee(self):
        with tempfile.TemporaryDirectory() as root:
            state = State(Path(root)/'state.json')
            state.record('NET-1', {'change_request_id': 49, 'status': None})
            writes = []
            class Jira:
                def update_fields(self, key, fields): writes.append(fields)
                def comment(self, key, text): pass
            class FF:
                def get(self, identifier):
                    return {'response': {'id': identifier, 'status': 'plan',
                                         'fields': [{'name': 'Owner',
                                                     'values': ['Unknown']}]}}
            settings = {'jira': {}, 'mirror': {
                'result_fields': {'owner': 'customfield_123'},
                'owner_assignees': {'alice': '712020:abc-123'}}}
            mirror(settings, FF(), state, Jira(), dry_run=False, log=lambda *a: None)
            self.assertEqual(writes, [{'customfield_123': 'Unknown'}])

    def test_saved_fields_are_delivered_before_fireflow_read(self):
        with tempfile.TemporaryDirectory() as root:
            state = State(Path(root)/'state.json')
            state.record('NET-1', {'change_request_id': 49,
                                   'pending_fields': {'customfield_123': '49'}})
            events = []
            class Jira:
                def update_fields(self, key, fields): events.append(('fields', fields))
                def comment(self, key, text): events.append(('comment', text))
            class FF:
                def get(self, identifier):
                    events.append(('read', identifier))
                    raise RuntimeError('offline')
            mirror({'jira': {}}, FF(), state, Jira(), dry_run=False, log=lambda *a: None)
            self.assertEqual(events[0], ('fields', {'customfield_123': '49'}))
            self.assertIsNone(state.entries()['NET-1']['pending_fields'])

    def test_failed_delivery_remains_durable(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'state.json'
            state = State(path)
            state.record('NET-1', {'change_request_id': 49,
                                   'pending_fields': {'customfield_123': '49'}})
            class Jira:
                def update_fields(self, key, fields): raise RuntimeError('offline')
            class FF:
                def get(self, identifier): raise AssertionError('must deliver pending fields first')
            result = mirror({'jira': {}}, FF(), state, Jira(), dry_run=False, log=lambda *a: None)
            self.assertTrue(result['failed'])
            self.assertEqual(State(path).entries()['NET-1']['pending_fields'], {'customfield_123': '49'})

    def test_unresolved_reports_pending_field_delivery(self):
        from algosec_jira_bus.bus import unresolved
        with tempfile.TemporaryDirectory() as root:
            state = State(Path(root)/'state.json')
            state.record('NET-1', {'change_request_id': 49, 'pending_fields': {'customfield_123': '49'}})
            self.assertEqual(unresolved(state)[0], ['NET-1'])

    def test_corrected_mapping_replays_saved_observation(self):
        with tempfile.TemporaryDirectory() as root:
            state = State(Path(root)/'state.json')
            state.record('NET-1', {'change_request_id': 49,
                'pending_fields': {'customfield_123': '49'},
                'pending_observation': {'id': 49, 'status': 'plan', 'details': {}}})
            writes = []
            class Jira:
                def update_fields(self, key, fields): writes.append(fields)
            class FF:
                def get(self, identifier): raise RuntimeError('offline')
            mirror({'jira': {}, 'mirror': {'result_fields': {'id': 'customfield_456'}}},
                   FF(), state, Jira(), dry_run=False, log=lambda *a: None)
            self.assertEqual(writes, [{'customfield_456': '49'}])

    def test_create_saves_id_delivery_before_any_status_read(self):
        from algosec_jira_bus.sync import run
        from tests.test_sync import issue, MAPPING
        with tempfile.TemporaryDirectory() as root:
            state = State(Path(root)/'state.json')
            class Jira:
                def search(self, *args, **kwargs): return [issue()]
            class FF:
                def create(self, request, operation, reason):
                    return {'response': {'data': {'id': 49}}, 'operation_id': operation}
            settings = {'jira': {'jql': 'project=NET'}, 'mapping': MAPPING,
                        'fireflow': {'template': 'T', 'devices': ['fw1']},
                        'mirror': {'result_fields': {'id': 'customfield_123'}}}
            run(settings, FF(), state, Jira(), dry_run=False, log=lambda *a: None)
            self.assertEqual(State(state.path).entries()['NET-12']['pending_fields'],
                             {'customfield_123': '49'})
