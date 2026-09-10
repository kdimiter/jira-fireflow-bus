"""Pin the shape FireFlow actually answers with, as documented for A33.20.

Every fixture here is modelled on the sample responses in the vendor's API guide
(`api-guide/get-ticket.md` and `api-guide/createatrafficchangerequest-request.md`), not on
what the bus finds convenient. The envelope's own "status" says whether the *call*
succeeded; the change request's status is a named entry in `data.fields`. Confusing the two
produced a bus that commented "is now Success" once per issue and then went quiet forever,
so these tests exist to keep that distinction from eroding again.

Marked throughout: this is the documented contract, verified against the reference. It has
not yet been seen on a live appliance -- that happens on .137 once the licence lands.
"""
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus.journal import Journal
from algosec_jira_bus.queue import Failures
from algosec_jira_bus.sync import (body_of, change_request_id, field_of, mirror,
                                   same_status, status_of, transition_for, State)


def envelope(data, outcome='Success'):
    """FireFlow's universal reply wrapper."""
    return {'response': {'status': outcome,
                         'messages': [{'code': 'success', 'message': 'Success'}],
                         'data': data}}


def ticket(identifier=20, status='implement', extra=()):
    fields = [{'name': 'Risk Level', 'values': ['No Risk']},
              {'name': 'Owner', 'values': ['admin<admin@example.test>']},
              {'name': 'Ticket Template Name', 'values': ['Basic Change Traffic Request']},
              {'name': 'Workflow', 'values': ['Basic']}]
    fields.extend(extra)
    if status is not None:
        fields.append({'name': 'status', 'values': [status]})
    return envelope({'id': identifier, 'subChangeRequests': [], 'fields': fields})


class Envelope(unittest.TestCase):
    def test_the_ticket_is_taken_from_data_not_from_the_wrapper(self):
        body = body_of(ticket())
        self.assertEqual(body['id'], 20)
        self.assertNotIn('messages', body)

    def test_an_envelope_without_data_yields_nothing_rather_than_itself(self):
        reply = {'response': {'status': 'Failure',
                              'messages': [{'code': 'NO_PERMISSIONS', 'message': 'denied'}]}}
        self.assertIsNone(body_of(reply))
        self.assertIsNone(status_of(reply), 'must never report the call outcome as a status')
        self.assertIsNone(change_request_id(reply))

    def test_a_reply_that_carries_no_envelope_is_the_ticket_itself(self):
        self.assertEqual(body_of({'response': {'id': 7, 'fields': []}})['id'], 7)

    def test_nothing_at_all_is_handled_without_raising(self):
        for empty in (None, {}, {'response': None}, {'response': 'text'}):
            self.assertIsNone(body_of(empty))
            self.assertIsNone(status_of(empty))
            self.assertIsNone(change_request_id(empty))


class Status(unittest.TestCase):
    def test_the_workflow_status_comes_from_the_fields_array(self):
        self.assertEqual(status_of(ticket(status='implement')), 'implement')

    def test_the_word_success_is_never_mistaken_for_a_status(self):
        self.assertNotEqual(status_of(ticket(status='resolved')), 'Success')
        self.assertIsNone(status_of(envelope({'id': 5, 'fields': []})),
                          'no status field means no status, not "Success"')

    def test_the_field_name_is_matched_ignoring_case(self):
        body = body_of(envelope({'fields': [{'name': 'Status', 'values': ['approved']}]}))
        self.assertEqual(field_of(body, 'status'), 'approved')

    def test_a_status_promoted_out_of_the_fields_array_is_still_read(self):
        self.assertEqual(status_of(envelope({'id': 5, 'status': 'validate'})), 'validate')

    def test_statuses_compare_without_regard_to_case(self):
        self.assertTrue(same_status('implement', 'Implement'))
        self.assertTrue(same_status(' Resolved ', 'resolved'))
        self.assertFalse(same_status('implement', 'resolved'))
        self.assertFalse(same_status(None, 'resolved'))

    def test_a_transition_configured_in_title_case_matches_a_lower_case_status(self):
        transitions = {'Implement': 'In Progress', 'Resolved': 'Done'}
        self.assertEqual(transition_for(transitions, 'resolved'), 'Done')
        self.assertEqual(transition_for(transitions, 'implement'), 'In Progress')
        self.assertIsNone(transition_for(transitions, 'plan'))
        self.assertIsNone(transition_for(None, 'plan'))


class RequestId(unittest.TestCase):
    def test_the_id_is_read_from_the_data_object(self):
        self.assertEqual(change_request_id(ticket(identifier=4595)), 4595)

    def test_the_documented_empty_create_response_yields_no_id(self):
        # The vendor's own success example for a create carries "data": {}. This must come
        # back as None so the caller can say so, not as a crash and not as a false id.
        self.assertIsNone(change_request_id(envelope({})))

    def test_a_numeric_string_id_is_accepted(self):
        self.assertEqual(change_request_id(envelope({'id': '77'})), 77)

    def test_a_boolean_is_not_mistaken_for_an_id(self):
        self.assertIsNone(change_request_id(envelope({'id': True})))

    def test_an_id_carried_as_a_field_is_still_found(self):
        self.assertEqual(change_request_id(envelope({'fields': [{'name': 'id', 'values': ['31']}]})), 31)


class Jira:
    def __init__(self): self.comments, self.transitions = [], []
    def comment(self, key, text): self.comments.append((key, text))
    def transition(self, key, name): self.transitions.append((key, name))


class Fireflow:
    def __init__(self, directory, replies):
        self.config = {'base_url': 'https://asms.example.test'}
        self.state = Path(directory)
        self.replies = replies
    def get(self, identifier): return self.replies[identifier]


class EndToEnd(unittest.TestCase):
    """The whole mirror pass against the documented reply, not against a convenient fake."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'receipts').mkdir(parents=True)
        self.state = State(self.root / 'state' / 'sync.json')
        self.failures = Failures(self.state)
        self.journal = Journal(self.root / 'state')
        self.settings = {'mirror': {'comment': 'FireFlow %(id)s is now %(status)s.',
                                    'transitions': {'Resolved': 'Done'}}}

    def tearDown(self):
        self.directory.cleanup()

    def run_mirror(self, fireflow, jira):
        return mirror(self.settings, fireflow, self.state, jira=jira, dry_run=False,
                      log=lambda *a: None, failures=self.failures, journal=self.journal)

    def test_the_real_status_reaches_the_issue_and_success_never_does(self):
        self.state.record('NET-12', {'change_request_id': 20, 'status': None})
        fireflow = Fireflow(self.root / 'receipts', {20: ticket(20, 'implement')})
        jira = Jira()
        self.run_mirror(fireflow, jira)
        self.assertEqual(len(jira.comments), 1)
        self.assertIn('is now implement', jira.comments[0][1])
        self.assertNotIn('Success', jira.comments[0][1])
        self.assertEqual(self.state.entries()['NET-12']['status'], 'implement')

    def test_a_status_that_only_differs_in_case_is_not_reported_twice(self):
        self.state.record('NET-12', {'change_request_id': 20, 'status': 'Implement',
                                      'workflow_details': {'Owner': 'admin<admin@example.test>'}})
        fireflow = Fireflow(self.root / 'receipts', {20: ticket(20, 'implement')})
        jira = Jira()
        result = self.run_mirror(fireflow, jira)
        self.assertEqual(jira.comments, [])
        self.assertEqual(result['unchanged'], ['NET-12'])

    def test_a_lower_case_status_fires_the_transition_configured_in_title_case(self):
        self.state.record('NET-12', {'change_request_id': 20, 'status': 'implement'})
        fireflow = Fireflow(self.root / 'receipts', {20: ticket(20, 'resolved')})
        jira = Jira()
        self.run_mirror(fireflow, jira)
        self.assertEqual(jira.transitions, [('NET-12', 'Done')])

    def test_a_missing_workflow_status_is_reported_without_changing_jira(self):
        self.state.record('NET-12', {'change_request_id': 20, 'status': 'plan'})
        fireflow = Fireflow(self.root / 'receipts', {20: ticket(20, status=None)})
        jira = Jira()
        result = self.run_mirror(fireflow, jira)
        self.assertEqual(jira.comments, [])
        self.assertEqual([key for key, _ in result['failed']], ['NET-12'])
        self.assertEqual(self.state.entries()['NET-12']['status'], 'plan')


if __name__ == '__main__':
    unittest.main()
