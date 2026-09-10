import json
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.jira import plain
from algosec_jira_bus.sync import MappingError, State, build, run, split


MAPPING = {'action': {'field': 'cf_action', 'values': {'Open': 'Allow', 'Close': 'Drop'}},
           'source': {'field': 'cf_src'}, 'destination': {'field': 'cf_dst'},
           'service': {'field': 'cf_svc'}}


def issue(key='NET-12', action='Open', source='192.0.2.0/24', destination='198.51.100.5',
          service='tcp/443', summary='Open access for billing'):
    return {'key': key, 'fields': {'summary': summary, 'cf_action': action, 'cf_src': source,
                                   'cf_dst': destination, 'cf_svc': service}}


class Fireflow:
    def __init__(self): self.calls = []
    def create(self, request, operation_id, reason):
        self.calls.append((request, operation_id, reason))
        return {'operation_id': operation_id, 'receipt': 'r1'}


class Jira:
    def __init__(self, issues): self.issues, self.queries = issues, []
    def search(self, jql, fields, limit=50):
        self.queries.append((jql, set(fields), limit))
        return self.issues


class Mapping(unittest.TestCase):
    def test_an_open_request_becomes_an_allow_line(self):
        key, request = build(issue(), MAPPING, 'Traffic Change Request', ['fw1'])
        self.assertEqual(key, 'NET-12')
        self.assertEqual(request['traffic'][0]['action'], 'Allow')
        self.assertEqual(request['traffic'][0]['source']['items'], [{'address': '192.0.2.0/24'}])
        self.assertEqual(request['traffic'][0]['service']['items'], [{'service': 'tcp/443'}])

    def test_a_close_request_becomes_a_drop_line(self):
        _, request = build(issue(action='Close'), MAPPING, 'T', ['fw1'])
        self.assertEqual(request['traffic'][0]['action'], 'Drop')

    def test_the_jira_key_travels_with_the_request(self):
        _, request = build(issue(), MAPPING, 'T', ['fw1'])
        fields = {item['name']: item['values'] for item in request['fields']}
        self.assertTrue(fields['subject'][0].startswith('NET-12:'))
        self.assertNotIn('externalId', fields)
        self.assertEqual(fields['devices'], ['fw1'])
        self.assertTrue(fields['subject'][0].startswith('NET-12: '))

    def test_lists_are_accepted_the_way_a_person_types_them(self):
        _, request = build(issue(source='192.0.2.1, 192.0.2.2\n192.0.2.3'), MAPPING, 'T', ['fw1'])
        self.assertEqual([item['address'] for item in request['traffic'][0]['source']['items']],
                         ['192.0.2.1', '192.0.2.2', '192.0.2.3'])

    def test_an_unmapped_action_is_refused_rather_than_guessed(self):
        with self.assertRaises(MappingError):
            build(issue(action='Maybe'), MAPPING, 'T', ['fw1'])

    def test_an_empty_or_unusable_value_is_refused(self):
        for change in ({'source': ''}, {'destination': 'a b"c'}, {'service': '$(whoami)'}):
            with self.assertRaises(MappingError):
                build(issue(**change), MAPPING, 'T', ['fw1'])

    def test_rich_text_fields_are_read_as_plain_text(self):
        document = {'type': 'doc', 'content': [{'type': 'paragraph',
                    'content': [{'type': 'text', 'text': '192.0.2.9'}]}]}
        self.assertEqual(plain(document), '192.0.2.9')
        self.assertEqual(plain({'value': 'Open'}), 'Open')
        self.assertEqual(plain(None), '')

    def test_splitting_ignores_empty_items(self):
        self.assertEqual(split(' a , , b \n'), ['a', 'b'])


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name)/'state.json')
        self.settings = {'jira': {'jql': 'project = NET'}, 'mapping': MAPPING,
                         'fireflow': {'template': 'T', 'devices': ['fw1']}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_dry_run_creates_nothing(self):
        fireflow = Fireflow()
        result = run(self.settings, fireflow, self.state, Jira([issue()]), dry_run=True, log=lambda *a: None)
        self.assertEqual(fireflow.calls, [])
        self.assertEqual(len(result['created']), 1)
        self.assertFalse(self.state.seen('NET-12'))

    def test_an_issue_is_only_ever_submitted_once(self):
        fireflow = Fireflow()
        jira = Jira([issue()])
        run(self.settings, fireflow, self.state, jira, dry_run=False, log=lambda *a: None)
        result = run(self.settings, fireflow, self.state, jira, dry_run=False, log=lambda *a: None)
        self.assertEqual(len(fireflow.calls), 1)
        self.assertEqual(result['skipped'], ['NET-12'])

    def test_the_operation_id_is_derived_from_the_issue_key(self):
        fireflow = Fireflow()
        run(self.settings, fireflow, self.state, Jira([issue()]), dry_run=False, log=lambda *a: None)
        self.assertEqual(fireflow.calls[0][1], 'jira-NET_12')
        self.assertIn('NET-12', fireflow.calls[0][2])

    def test_one_bad_issue_does_not_stop_the_others(self):
        fireflow = Fireflow()
        result = run(self.settings, fireflow, self.state,
                     Jira([issue(key='NET-1', action='Nonsense'), issue(key='NET-2')]),
                     dry_run=False, log=lambda *a: None)
        self.assertEqual([key for key, _ in result['refused']], ['NET-1'])
        self.assertEqual(len(fireflow.calls), 1)

    def test_only_the_mapped_fields_are_requested_from_jira(self):
        jira = Jira([])
        run(self.settings, Fireflow(), self.state, jira, dry_run=True, log=lambda *a: None)
        self.assertEqual(jira.queries[0][1], {'cf_action', 'cf_src', 'cf_dst', 'cf_svc', 'summary', 'creator', 'reporter'})

    def test_state_survives_a_restart(self):
        fireflow = Fireflow()
        run(self.settings, fireflow, self.state, Jira([issue()]), dry_run=False, log=lambda *a: None)
        reopened = State(self.state.path)
        self.assertTrue(reopened.seen('NET-12'))
        self.assertEqual(self.state.path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()


class MirrorBack(unittest.TestCase):
    """FireFlow moves through Request, Plan, Approve, Review, Implement, Validate, Resolved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name)/'state.json')
        self.state.record('NET-12', {'change_request_id': 49, 'status': None})
        self.settings = {'jira': {}, 'mirror': {'comment': 'FireFlow %(id)s: %(status)s'}}

    def tearDown(self):
        self.tmp.cleanup()

    class Fireflow:
        def __init__(self, status): self.status, self.reads = status, []
        def get(self, identifier):
            self.reads.append(identifier)
            return {'response': {'id': identifier, 'status': self.status}}

    class Jira:
        def __init__(self, transitions=('Done',)):
            self.comments, self.moves, self.transitions = [], [], transitions
        def comment(self, key, text): self.comments.append((key, text))
        def transition(self, key, name):
            if name not in self.transitions:
                raise ValueError('no such transition')
            self.moves.append((key, name))

    def mirror(self, status, dry_run=False, settings=None):
        from algosec_jira_bus.sync import mirror
        fireflow, jira = self.Fireflow(status), self.Jira()
        result = mirror(settings or self.settings, fireflow, self.state, jira,
                        dry_run=dry_run, log=lambda *a: None)
        return result, fireflow, jira

    def test_a_new_status_is_reported_once(self):
        result, _, jira = self.mirror('Approve')
        self.assertEqual(result['updated'], [('NET-12', 'Approve')])
        self.assertEqual(jira.comments, [('NET-12', 'FireFlow 49: Approve')])
        # Polling again with the same status must stay quiet.
        result, _, jira = self.mirror('Approve')
        self.assertEqual(result['unchanged'], ['NET-12'])
        self.assertEqual(jira.comments, [])

    def test_each_further_stage_is_reported_in_turn(self):
        seen = []
        for stage in ('Plan', 'Approve', 'Implement', 'Resolved'):
            result, _, _ = self.mirror(stage)
            seen += [status for _, status in result['updated']]
        self.assertEqual(seen, ['Plan', 'Approve', 'Implement', 'Resolved'])

    def test_a_dry_run_tells_jira_nothing(self):
        result, _, jira = self.mirror('Implement', dry_run=True)
        self.assertEqual(jira.comments, [])
        self.assertEqual(result['updated'], [('NET-12', 'Implement')])
        self.assertIsNone(self.state.entries()['NET-12']['status'])

    def test_a_transition_is_optional_and_its_failure_is_not_fatal(self):
        settings = dict(self.settings)
        settings['mirror'] = {'comment': 'x %(status)s', 'transitions': {'Resolved': 'Nonexistent'}}
        result, _, jira = self.mirror('Resolved', settings=settings)
        self.assertEqual(len(jira.comments), 1)
        self.assertEqual(jira.moves, [])
        self.assertTrue(result['failed'])
        # The status was still recorded, so it is not reported again on the next poll.
        self.assertEqual(self.state.entries()['NET-12']['status'], 'Resolved')

    def test_an_unreadable_request_is_reported_and_skipped(self):
        from algosec_jira_bus.sync import mirror

        class Broken:
            def get(self, identifier): raise RuntimeError('gone')

        result = mirror(self.settings, Broken(), self.state, self.Jira(), dry_run=False,
                        log=lambda *a: None)
        self.assertEqual([key for key, _ in result['failed']], ['NET-12'])

    def test_entries_without_a_request_id_are_ignored(self):
        self.state.record('NET-99', {'status': None})
        _, fireflow, _ = self.mirror('Plan')
        self.assertEqual(fireflow.reads, [49])

    def test_the_created_id_is_found_in_either_response_shape(self):
        from algosec_jira_bus.sync import change_request_id, status_of
        self.assertEqual(change_request_id({'response': {'id': 7}}), 7)
        self.assertEqual(change_request_id({'response': {'data': {'changeRequestId': '11'}}}), 11)
        self.assertIsNone(change_request_id({'response': {'nothing': True}}))
        self.assertEqual(status_of({'response': {'status': 'Plan'}}), 'Plan')
        self.assertEqual(status_of({'response': {'data': {'Status': {'value': 'Approve'}}}}), 'Approve')
