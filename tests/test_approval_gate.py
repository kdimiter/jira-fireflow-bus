import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from algosec_jira_bus.approval import ApprovalError
from algosec_jira_bus.approval_gate import ApprovalLedger
from algosec_jira_bus.jira import Jira, JiraError


def issue():
    return {'id': '1234', 'key': 'NET-1', 'fields': {
        'status': {'name': 'Approved'}, 'summary': 'Test',
        'customfield_1': {'schemaVersion': 1, 'justification': 'Business need',
                          'changeType': 'Allow', 'trafficLines': [{
            'source': {'kind': 'subnet', 'value': '203.0.113.0/24'},
            'destination': {'kind': 'ip', 'value': '192.0.2.1'},
            'services': [{'kind': 'port', 'protocol': 'tcp', 'port': 443}]}]}}}


class FakeJira:
    config = {'base_url': 'https://example.atlassian.net'}

    def __init__(self):
        self.issue = issue()
        self.reads = []

    def read_issue(self, identifier, fields):
        self.reads.append((identifier, fields))
        return copy.deepcopy(self.issue)


class ApprovalGate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'approvals'
        self.ledger = ApprovalLedger(self.directory)
        self.jira = FakeJira()

    def capture(self):
        return self.ledger.capture(self.jira, '1234', 'customfield_1', 'Шаблон',
                                   ['device1'], operator='pilot-admin')

    def verify(self, **overrides):
        args = dict(jira=self.jira, identifier='1234', field='customfield_1',
                    template='Шаблон', devices=['device1'])
        args.update(overrides)
        return self.ledger.verify(**args)

    def test_capture_and_verify_fetch_fresh_issue_and_protect_storage(self):
        record = self.capture()
        fresh = self.verify()
        self.assertEqual(record['captured_by'], 'pilot-admin')
        self.assertEqual(len(self.jira.reads), 2)
        self.assertEqual(fresh['id'], '1234')
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / '1234.json').stat().st_mode & 0o777, 0o600)
        fresh['fields']['status']['name'] = 'Changed'
        self.assertEqual(self.jira.issue['fields']['status']['name'], 'Approved')

    def test_missing_capture_rejects(self):
        with self.assertRaises(ApprovalError):
            self.verify()

    def test_changed_content_requires_recapture(self):
        self.capture()
        self.jira.issue['fields']['customfield_1']['trafficLines'][0]['destination']['value'] = '192.0.2.2'
        with self.assertRaises(ApprovalError):
            self.verify()
        self.capture()
        self.verify()

    def test_changed_template_devices_and_origin_reject(self):
        self.capture()
        for override in ({'template': 'Other'}, {'devices': ['device2']}):
            with self.subTest(override=override), self.assertRaises(ApprovalError):
                self.verify(**override)
        self.jira.config = {'base_url': 'https://different.atlassian.net'}
        with self.assertRaises(ApprovalError):
            self.verify()

    def test_changed_status_rejects_capture_and_verification(self):
        self.capture()
        self.jira.issue['fields']['status']['name'] = 'To Do'
        for operation in (self.capture, self.verify):
            with self.subTest(operation=operation), self.assertRaises(ApprovalError):
                operation()

    def test_same_key_with_different_id_cannot_use_approval(self):
        self.capture()
        self.jira.issue['id'] = '5678'
        with self.assertRaises(ApprovalError):
            self.verify()
        with self.assertRaises(ApprovalError):
            self.verify(identifier='5678')

    def test_key_rename_preserves_identity_binding(self):
        self.capture()
        self.jira.issue['key'] = 'MOVED-10'
        self.assertEqual(self.verify()['key'], 'MOVED-10')

    def test_normalized_whitespace_does_not_revoke_approval(self):
        self.capture()
        self.jira.issue['fields']['customfield_1']['justification'] = ' Business need '
        self.verify()

    def test_public_file_or_directory_rejected(self):
        self.capture()
        os.chmod(self.directory / '1234.json', 0o644)
        with self.assertRaises(ApprovalError):
            self.verify()
        os.chmod(self.directory, 0o755)
        with self.assertRaises(ApprovalError):
            ApprovalLedger(self.directory)

    def test_symlink_record_rejected(self):
        self.capture()
        original = self.directory / '1234.json'
        replacement = self.directory / 'other.json'
        original.rename(replacement)
        original.symlink_to(replacement)
        with self.assertRaises(ApprovalError):
            self.verify()

    def test_read_issue_uses_numeric_id_and_requested_fields(self):
        jira = Jira({'base_url': 'https://example.atlassian.net', 'email': 'test', 'token_ref': 'test'})
        with patch.object(jira, '_call', return_value=issue()) as call:
            jira.read_issue('1234', {'status', 'customfield_1'})
            call.assert_called_once_with('/rest/api/3/issue/1234',
                                         query={'fields': 'customfield_1,status'})
        with self.assertRaises(JiraError):
            jira.read_issue('NET-1', {'status'})
