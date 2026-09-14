import json
import tempfile
import unittest
from pathlib import Path
from algosec_jira_bus.sync import State, mirror

class WorkflowSnapshot(unittest.TestCase):
    def test_basic_example_collapses_every_active_fireflow_stage_into_in_work(self):
        config = json.loads((Path(__file__).resolve().parents[1] /
                             'examples/jira-sync-basic-structured.json').read_text())
        transitions = config['mirror']['transitions']
        for status in ('open', 'plan', 'approve', 'approved', 'check',
                       'implementation plan', 'create work order', 'implement',
                       'validate', 'user accept', 'reconcile', 'pending match', 'match'):
            self.assertEqual(transitions[status], 'In Work')
        self.assertEqual(transitions['rejected'], 'Rejected / Cancelled')
        self.assertEqual(transitions['cancelled'], 'Rejected / Cancelled')

    def test_owner_change_is_delivered_without_repeating_transition(self):
        with tempfile.TemporaryDirectory() as d:
            state = State(Path(d)/'state.json')
            state.record('NET-1', {'change_request_id': 1})
            class FF:
                owner = 'Alice'
                def get(self, identifier):
                    return {'response': {'data': {'fields': [
                        {'name': 'status', 'values': ['plan']},
                        {'name': 'Owner', 'values': [self.owner]}]}}}
            class Jira:
                comments = []
                moves = []
                def comment(self, key, text): self.comments.append(text)
                def transition(self, key, target): self.moves.append(target)
            ff, jira = FF(), Jira()
            cfg = {'jira': {}, 'mirror': {'transitions': {'plan': 'In Progress'}}}
            for owner in ('Alice', 'Bob', 'Bob'):
                ff.owner = owner
                mirror(cfg, ff, state, jira, dry_run=False, log=lambda *a: None)
            self.assertEqual(len(jira.comments), 2)
            self.assertIn('Bob', jira.comments[-1])
            self.assertEqual(jira.moves, ['In Progress'])

    def test_outcome_rule_requires_exact_evidence(self):
        from algosec_jira_bus.sync import workflow_transition
        config = {'transitions': {}, 'outcome_rules': [{
            'status': 'resolved', 'field': 'Closure reason',
            'equals': 'Already allowed', 'transition': 'Done'}]}
        self.assertIsNone(workflow_transition(config, 'resolved', {}))
        self.assertIsNone(workflow_transition(config, 'resolved', {'Closure reason': 'Rejected'}))
        self.assertEqual(workflow_transition(config, 'resolved', {'Closure reason': 'Already allowed'}), 'Done')

    def test_already_works_history_completes_resolved_request_without_validation_result(self):
        with tempfile.TemporaryDirectory() as d:
            state = State(Path(d) / 'state.json')
            state.record('NET-59', {'change_request_id': 59})

            class FF:
                def get(self, identifier):
                    return {'response': {'status': 'Success', 'data': {
                        'id': identifier, 'subChangeRequests': [], 'fields': [
                            {'name': 'status', 'values': ['resolved']},
                            {'name': 'Initial Plan status', 'values': ['Result OK']},
                        ]}}}

                def rt_terminal_outcome(self, identifier):
                    self.identifier = identifier
                    return 'already works'

            class Jira:
                def __init__(self):
                    self.comments, self.moves = [], []

                def comment(self, key, text):
                    self.comments.append((key, text))

                def transition(self, key, target):
                    self.moves.append((key, target))

            ff, jira = FF(), Jira()
            cfg = {'jira': {}, 'mirror': {
                'verify_resolved_children': True,
                'outcome_rules': [
                    {'status': 'resolved', 'field': 'Completion verified',
                     'equals': 'yes', 'transition': 'Done'},
                    {'status': 'resolved', 'field': 'Completion outcome',
                     'equals': 'already works', 'transition': 'Done'},
                ],
            }}
            result = mirror(cfg, ff, state, jira, dry_run=False, log=lambda *_a: None)
            self.assertEqual(result['failed'], [])
            self.assertEqual(ff.identifier, 59)
            self.assertEqual(jira.moves, [('NET-59', 'Done')])
            saved = state.entries()['NET-59']['workflow_details']
            self.assertEqual(saved['Completion verified'], 'no')
            self.assertEqual(saved['Completion outcome'], 'already works')

    def test_blank_outcome_cannot_authorize_completion(self):
        from algosec_jira_bus.sync import validate_mirror, MappingError
        with self.assertRaises(MappingError):
            validate_mirror({'outcome_rules': [{'status': 'resolved', 'field': 'Result',
                                              'equals': '', 'transition': 'Done'}]})

    def test_failure_envelope_does_not_publish_stale_status(self):
        from algosec_jira_bus.sync import status_of
        self.assertIsNone(status_of({'response': {'status': 'Failure', 'messages': [],
                                                 'data': {'status': 'resolved'}}}))
