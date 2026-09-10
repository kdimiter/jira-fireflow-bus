import copy
import unittest

from algosec_jira_bus.approval import ApprovalError, request_hash, verify_approved


def request():
    return {'schemaVersion': 1, 'justification': 'Business need', 'changeType': 'Allow',
            'trafficLines': [{'source': {'kind': 'subnet', 'value': '203.0.113.0/24'},
                              'destination': {'kind': 'ip', 'value': '192.0.2.1'},
                              'services': [{'kind': 'port', 'protocol': 'tcp', 'port': 443}]}]}


class Approval(unittest.TestCase):
    def test_unchanged_request_returns_normalized_snapshot(self):
        raw = request()
        approved = verify_approved(raw, request_hash(raw))
        self.assertEqual(approved['action'], 'Allow')
        self.assertEqual(approved['lines'][0]['services'], ['tcp/443'])

    def test_key_order_and_normalized_whitespace_do_not_invalidate_approval(self):
        raw = request()
        reordered = dict(reversed(list(raw.items())))
        reordered['justification'] = ' Business need '
        reordered['duration'] = {'kind': 'permanent'}
        self.assertEqual(request_hash(raw), request_hash(reordered))

    def test_changes_to_approved_request_are_rejected(self):
        raw = request()
        edits = [lambda x: x.update(changeType='Drop'),
                 lambda x: x.update(justification='Another reason'),
                 lambda x: x['trafficLines'][0]['destination'].update(value='192.0.2.2'),
                 lambda x: x['trafficLines'][0]['services'][0].update(port=8443),
                 lambda x: x['trafficLines'].append(copy.deepcopy(x['trafficLines'][0]))]
        for edit in edits:
            with self.subTest(edit=edit):
                changed = copy.deepcopy(raw)
                edit(changed)
                with self.assertRaises(ApprovalError):
                    verify_approved(changed, request_hash(raw))

    def test_missing_or_malformed_approved_hash_is_rejected(self):
        for digest in (None, '', 'f' * 64, 'sha256:' + 'z' * 64, True):
            with self.subTest(digest=digest), self.assertRaises(ApprovalError):
                verify_approved(request(), digest)

    def test_unsupported_or_invalid_schema_rejected_before_hashing(self):
        for version in (2, True, '1'):
            raw = request()
            raw['schemaVersion'] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                request_hash(raw)

    def test_snapshot_is_detached_from_caller_data(self):
        raw = request()
        snapshot = verify_approved(raw, request_hash(raw))
        raw['trafficLines'][0]['destination']['value'] = '192.0.2.2'
        self.assertEqual(snapshot['lines'][0]['destination']['value'], '192.0.2.1')
