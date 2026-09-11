import unittest

from copy import deepcopy

from algosec_jira_bus.jira_updates import (ORIGIN_PROPERTY, sync_updates,
                                          validate_jira_to_fireflow)
from algosec_jira_bus.sync import mirror


class Configuration(unittest.TestCase):
    def test_disabled_by_default(self):
        for value in (None, False, {}, {'enabled': False}):
            self.assertIsNone(validate_jira_to_fireflow(value))

    def test_requires_boolean_flags_and_explicit_string_map(self):
        for value in (True, {'enabled': 'false'}, {'enabled': True, 'comments': 'true'},
                      {'enabled': True, 'status_map': []},
                      {'enabled': True, 'status_map': {'Done': ''}}):
            with self.assertRaises(ValueError):
                validate_jira_to_fireflow(value)

    def test_privileged_or_invalid_fireflow_statuses_are_rejected(self):
        for target in ('approve', 'implement', 'Open', 'open_status'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_jira_to_fireflow({
                    'enabled': True, 'status_map': {'Jira state': target}})

    def test_enabled_does_not_implicitly_enable_writes(self):
        self.assertEqual(validate_jira_to_fireflow({'enabled': True}),
                         {'enabled': True, 'comments': False, 'status_map': {}})


class State:
    def __init__(self):
        self.issues = {'NET-1': {'jira_issue_id': '10001', 'change_request_id': 51,
                                  'status': 'old', 'pending_fields': {'field': 'value'}}}
        self.writes = []
        self.fail_save = False

    def entries(self):
        return deepcopy(self.issues)

    def record(self, key, update, clear_failure=False):
        if self.fail_save:
            self.fail_save = False
            raise OSError('simulated interrupted state save')
        self.writes.append(deepcopy(update))
        self.issues[key].update(deepcopy(update))


class Jira:
    def __init__(self):
        self.comment_rows = []
        self.histories = []
        self.reads = []
        self.outbound_comments = []
        self.transitions = []

    def comments(self, issue_id, after_id=None):
        self.reads.append(('comments', issue_id))
        return deepcopy(self.comment_rows)

    def changelog(self, issue_id, after_id=None):
        self.reads.append(('changelog', issue_id))
        return deepcopy(self.histories)

    def comment(self, key, text):
        self.outbound_comments.append((key, text))

    def transition(self, key, target):
        self.transitions.append((key, target))


class FireFlow:
    def __init__(self):
        self.calls = []
        self.applied = {}
        self.fail = False
        self.status = 'old'

    def _write(self, kind, ticket, value, operation, reason):
        self.calls.append((kind, ticket, value, operation, reason))
        if operation in self.applied:
            return self.applied[operation]
        self.applied[operation] = {'success': True}
        if self.fail:
            self.fail = False
            raise TimeoutError('reply lost after applying')
        return self.applied[operation]

    def add_comment(self, *args):
        return self._write('internal-comment', *args)

    def set_status(self, *args):
        result = self._write('status', *args)
        self.status = args[1]
        return result

    def get(self, ticket):
        return {'response': {'id': ticket, 'status': self.status}}


def comment(identifier, text='Який статус?'):
    return {'id': str(identifier), 'text': text,
            'body': {'type': 'doc', 'content': [
        {'type': 'paragraph', 'content': [{'type': 'text', 'text': text}]}]},
        'author': {'displayName': 'Reporter'}, 'created': '2026-09-11T12:00:00Z'}


def history(identifier, target='Done', target_id='100'):
    return {'id': str(identifier), 'from': 'In progress', 'to': target,
            'from_id': '3', 'to_id': target_id, 'kind': 'status'}


class Synchronization(unittest.TestCase):
    def setUp(self):
        self.state, self.jira, self.fireflow = State(), Jira(), FireFlow()
        self.settings = {'jira_to_fireflow': {'enabled': True, 'comments': True,
                                             'status_map': {'Done': 'resolved',
                                                            'Reopened': 'open'}}}

    def run_sync(self, dry_run=False):
        return sync_updates(self.settings, self.fireflow, self.state,
                            self.jira, dry_run=dry_run, log=lambda _: None)

    def test_disabled_does_not_read_or_write(self):
        self.settings = {}
        self.run_sync()
        self.assertFalse(self.jira.reads)
        self.assertFalse(self.state.writes)

    def test_bootstrap_skips_all_existing_history(self):
        self.jira.comment_rows = [comment(1)]
        self.jira.histories = [history(2)]
        self.assertEqual(self.run_sync()['bootstrapped'], ['NET-1'])
        self.assertFalse(self.fireflow.calls)
        self.assertEqual(self.state.issues['NET-1']['status'], 'old')
        self.assertEqual(self.state.issues['NET-1']['pending_fields'], {'field': 'value'})

    def test_new_events_delivered_once_and_internal_with_author(self):
        self.run_sync()
        self.jira.comment_rows = [comment(1)]
        self.jira.histories = [history(2), history(3, 'Reopened')]
        self.assertEqual(self.run_sync()['updated'], ['NET-1'])
        self.run_sync()
        self.assertEqual(len(self.fireflow.calls), 3)
        self.assertEqual(self.fireflow.calls[0][0], 'internal-comment')
        self.assertIn('Reporter', self.fireflow.calls[0][2])
        self.assertIn('Який статус?', self.fireflow.calls[0][2])
        self.assertEqual([c[2] for c in self.fireflow.calls[1:]], ['resolved', 'open'])

    def test_dry_run_neither_bootstraps_nor_consumes_events(self):
        self.run_sync(dry_run=True)
        self.assertFalse(self.state.writes)
        self.run_sync()
        before = deepcopy(self.state.issues)
        self.jira.comment_rows = [comment(1)]
        self.run_sync(dry_run=True)
        self.assertEqual(self.state.issues, before)
        self.assertFalse(self.fireflow.calls)
        self.run_sync()
        self.assertEqual(len(self.fireflow.calls), 1)

    def test_origin_properties_and_history_metadata_prevent_echo(self):
        self.run_sync()
        row = comment(1)
        row['properties'] = [{'key': ORIGIN_PROPERTY, 'value': {'source': 'fireflow'}}]
        status = history(2)
        status['historyMetadata'] = {'extraData': {ORIGIN_PROPERTY: 'fireflow'}}
        self.jira.comment_rows, self.jira.histories = [row], [status]
        self.run_sync()
        self.assertFalse(self.fireflow.calls)
        self.assertEqual(self.state.issues['NET-1']['jira_sync']['comments']['seen'], ['1'])

    def test_restricted_jira_comments_are_consumed_without_crossing_audience(self):
        self.run_sync()
        role_only = comment(1)
        role_only['visibility'] = {'type': 'role', 'value': 'Administrators'}
        jsm_internal = comment(2)
        jsm_internal['properties'] = [
            {'key': 'sd.public.comment', 'value': {'internal': True}}]
        self.jira.comment_rows = [role_only, jsm_internal]
        self.run_sync()
        self.assertFalse(self.fireflow.calls)
        self.assertEqual(self.state.issues['NET-1']['jira_sync']['comments']['seen'],
                         ['2'])

    def test_control_characters_are_replaced_before_persisting_a_comment(self):
        self.run_sync()
        self.jira.comment_rows = [comment(1, 'hello\x07world')]
        self.run_sync()
        self.assertIn('hello\ufffdworld', self.fireflow.calls[0][2])
        pending_writes = [write for write in self.state.writes
                          if (write.get('jira_sync', {}).get('comments', {})
                              .get('pending'))]
        self.assertIn('hello\ufffdworld', pending_writes[-1]['jira_sync']['comments']
                      ['pending']['value'])

    def test_cursor_is_compacted_to_one_high_water_id(self):
        self.run_sync()
        self.jira.comment_rows = [comment(1), comment(2), comment(3)]
        self.run_sync()
        self.assertEqual(self.state.issues['NET-1']['jira_sync']['comments']['seen'], ['3'])

    def test_normalized_mentions_are_kept_and_long_comments_are_bounded(self):
        self.run_sync()
        row = comment(1, 'Hello @Bob ' + 'x' * 5000)
        row['body'] = {'type': 'doc', 'content': [{'type': 'paragraph', 'content': [
            {'type': 'text', 'text': 'Hello '},
            {'type': 'mention', 'attrs': {'text': '@Bob'}}]}]}
        self.jira.comment_rows = [row]
        self.run_sync()
        mirrored = self.fireflow.calls[0][2]
        self.assertIn('Hello @Bob', mirrored)
        self.assertIn('[truncated by jira-fireflow-bus]', mirrored)
        self.assertLessEqual(len(mirrored), 4000)

    def test_unmapped_status_is_consumed_without_guessing(self):
        self.run_sync()
        self.jira.histories = [history(1, 'In progress')]
        self.run_sync()
        self.assertFalse(self.fireflow.calls)
        self.assertEqual(self.state.issues['NET-1']['jira_sync']['statuses']['seen'], ['1'])

    def test_status_id_mapping_takes_precedence(self):
        self.settings['jira_to_fireflow']['status_map']['100'] = 'rejected'
        self.run_sync()
        self.jira.histories = [history(1)]
        self.run_sync()
        self.assertEqual(self.fireflow.calls[0][2], 'rejected')

    def test_jira_origin_status_is_not_immediately_transitioned_back_by_mirror(self):
        self.run_sync()
        self.jira.histories = [history(1)]
        self.run_sync()
        self.state.record('NET-1', {'pending_fields': None})
        self.assertIn('mirror_suppression',
                      self.state.issues['NET-1']['jira_sync'])
        settings = {**self.settings,
                    'mirror': {'transitions': {'resolved': 'Done'},
                               'comment': 'FireFlow %(id)s: %(status)s'}}
        class NoFailures:
            def blocked(self, _key): return False
            def entry(self, _key): return None
            def clear(self, _key): return None
            def parked(self): return {}
        mirror(settings, self.fireflow, self.state, jira=self.jira,
               dry_run=False, log=lambda *_: None, failures=NoFailures())
        self.assertEqual(self.jira.transitions, [])
        self.assertEqual(len(self.jira.outbound_comments), 1)
        entry = self.state.issues['NET-1']
        self.assertNotIn('mirror_suppression', entry['jira_sync'])
        self.assertIsNone(entry['pending_transition'])
        self.assertEqual(entry['workflow_target'], 'Done')

    def test_unknown_write_retries_same_operation_without_duplicate(self):
        self.run_sync()
        self.jira.comment_rows = [comment(1), comment(2)]
        self.fireflow.fail = True
        self.assertEqual(self.run_sync()['failed'], ['NET-1'])
        self.assertEqual(self.state.issues['NET-1']['jira_sync']['comments']['seen'], [])
        self.run_sync()
        self.assertEqual(self.fireflow.calls[0][3], self.fireflow.calls[1][3])
        self.assertEqual(len(self.fireflow.applied), 2)

    def test_unknown_write_then_jira_key_rename_keeps_exact_payload(self):
        self.run_sync()
        self.jira.comment_rows = [comment(1)]
        self.fireflow.fail = True
        self.assertEqual(self.run_sync()['failed'], ['NET-1'])
        self.state.issues['MOVED-1'] = self.state.issues.pop('NET-1')
        self.run_sync()
        self.assertEqual(self.fireflow.calls[0], self.fireflow.calls[1])
        self.assertEqual(len(self.fireflow.applied), 1)

    def test_enabling_second_stream_bootstraps_it_separately(self):
        self.settings['jira_to_fireflow']['comments'] = False
        self.run_sync()
        self.settings['jira_to_fireflow']['comments'] = True
        self.jira.comment_rows = [comment(1)]
        self.jira.histories = [history(2)]
        self.run_sync()
        self.assertEqual(len(self.fireflow.calls), 1)
        self.assertEqual(self.fireflow.calls[0][0], 'status')

    def test_missing_immutable_issue_id_and_malformed_stream_fail_closed(self):
        self.state.issues['NET-1'].pop('jira_issue_id')
        self.assertEqual(self.run_sync()['failed'], ['NET-1'])
        self.assertFalse(self.jira.reads)
        self.state.issues['NET-1']['jira_issue_id'] = '10001'
        self.jira.histories = [{'id': 'not-an-id'}]
        self.assertEqual(self.run_sync()['failed'], ['NET-1'])
        self.assertFalse(self.state.writes)

    def test_pending_comment_delivered_after_jira_comment_is_deleted(self):
        self.run_sync()
        self.jira.comment_rows = [comment(1)]
        self.fireflow.fail = True
        self.run_sync()
        self.jira.comment_rows = []
        self.run_sync()
        self.assertEqual(self.fireflow.calls[0], self.fireflow.calls[1])
        self.assertNotIn('pending', self.state.issues['NET-1']['jira_sync']['comments'])

    def test_failed_intent_save_never_calls_remote(self):
        self.run_sync()
        self.jira.comment_rows = [comment(1)]
        self.state.fail_save = True
        self.assertEqual(self.run_sync()['failed'], ['NET-1'])
        self.assertFalse(self.fireflow.calls)
        self.run_sync()
        self.assertEqual(len(self.fireflow.calls), 1)

    def test_unknown_outcome_parks_without_repeated_adapter_calls(self):
        class FireFlowMutationUnknown(ValueError):
            pass

        self.run_sync()
        self.jira.comment_rows = [comment(1)]
        calls = []

        def unknown(*args):
            calls.append(args)
            raise FireFlowMutationUnknown('Inspect receipt')

        self.fireflow.add_comment = unknown
        self.assertEqual(self.run_sync()['parked'], ['NET-1'])
        self.assertEqual(self.run_sync()['parked'], ['NET-1'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state.issues['NET-1']['jira_sync']['comments']['seen'], [])
