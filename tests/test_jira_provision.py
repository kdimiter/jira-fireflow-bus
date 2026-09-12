import unittest
from unittest.mock import patch

from algosec_jira_bus.jira_provision import (
    JiraProvisionError,
    SPACE_TEMPLATE,
    TEXT_TYPE,
    ensure_space,
    prepare_jira,
)


class JiraProvisionTests(unittest.TestCase):
    def fixture(self, initial_project=None):
        calls = []
        project = initial_project
        fields = [
            {'id': 'customfield_10000', 'name': 'Мережеві доступи AlgoSec',
             'schema': {'custom': 'ari:cloud:ecosystem::extension/app/static/algosec-network-access'}},
        ]
        ids = iter(range(20000, 20100))
        def call(path, **kwargs):
            nonlocal project
            calls.append((path, kwargs))
            if path.endswith('/myself'): return {'accountId': 'admin-account'}
            if path.endswith('/mypermissions'):
                return {'permissions': {'ADMINISTER': {'havePermission': True}}}
            if path.endswith('/projectvalidate/key'):
                return {'errorMessages': [], 'errors': {}}
            if path.endswith('/projectvalidate/validProjectName'):
                return kwargs['query']['name']
            if path.startswith('/rest/api/3/project/'): return project
            if path == '/rest/api/3/project':
                project = {'id': '10001', 'key': kwargs['body']['key'],
                           'name': kwargs['body']['name'], 'issueTypes': [],
                           'simplified': False}
                return project
            if path == '/rest/api/3/issuetype' and kwargs.get('method') != 'POST': return []
            if path == '/rest/api/3/issuetype': return {'id': '10002', 'name': 'Network Access'}
            if path.endswith('/issuetypescheme/project'):
                return {'values': [{'issueTypeScheme': {'id': '10003'}}]}
            if path == '/rest/api/3/field' and kwargs.get('method') != 'POST': return fields
            if path == '/rest/api/3/field':
                item = {'id': 'customfield_' + str(next(ids)),
                        'name': kwargs['body']['name'], 'schema': {'custom': TEXT_TYPE}}
                fields.append(item); return item
            if path == '/rest/api/3/screens' and kwargs.get('method') != 'POST':
                return {'values': []}
            if path == '/rest/api/3/screens': return {'id': '10004'}
            if path.endswith('/tabs'): return [{'id': '10005'}]
            if path.endswith('/fields') and kwargs.get('method') != 'POST': return []
            if path == '/rest/api/3/screenscheme' and kwargs.get('method') != 'POST':
                return {'values': []}
            if path == '/rest/api/3/screenscheme': return {'id': '10006'}
            if path == '/rest/api/3/issuetypescreenscheme' and kwargs.get('method') != 'POST':
                return {'values': []}
            if path == '/rest/api/3/issuetypescreenscheme': return {'id': '10007'}
            return {}
        return call, calls

    def test_space_only_apply_creates_company_managed_space(self):
        call, calls = self.fixture()
        result = ensure_space(call, apply=True, project_key='NET',
                              project_name='Network Access')
        self.assertEqual(result, {
            'ready': True, 'created': True, 'space_key': 'NET',
            'space_name': 'Network Access', 'project_id': '10001'})
        body = next(options['body'] for path, options in calls
                    if path == '/rest/api/3/project')
        self.assertEqual(body['projectTypeKey'], 'business')
        self.assertEqual(body['projectTemplateKey'], SPACE_TEMPLATE)
        self.assertEqual(body['leadAccountId'], 'admin-account')

    def test_space_only_dry_run_validates_without_mutation(self):
        call, calls = self.fixture()
        result = ensure_space(call, apply=False)
        self.assertEqual(result['planned'], ['space:ALGO'])
        self.assertFalse(result['ready'])
        self.assertFalse(any(options.get('method') in ('POST', 'PUT')
                             for _path, options in calls))

    def test_existing_space_is_reused_without_mutation(self):
        calls = []
        def call(path, **kwargs):
            calls.append((path, kwargs))
            if path.endswith('/myself'): return {'accountId': 'admin-account'}
            if path.endswith('/mypermissions'):
                return {'permissions': {'ADMINISTER': {'havePermission': True}}}
            if '/project/ALGO' in path:
                return {'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
                        'simplified': False}
            return {}
        result = ensure_space(call, apply=True)
        self.assertTrue(result['ready'])
        self.assertFalse(result['created'])
        self.assertFalse(any(options.get('method') in ('POST', 'PUT')
                             for _path, options in calls))

    def test_team_managed_space_is_rejected(self):
        def call(path, **_kwargs):
            if path.endswith('/myself'): return {'accountId': 'admin-account'}
            if path.endswith('/mypermissions'):
                return {'permissions': {'ADMINISTER': {'havePermission': True}}}
            return {'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
                    'simplified': True}
        with self.assertRaisesRegex(JiraProvisionError, 'company-managed'):
            ensure_space(call, apply=True)

    def test_existing_key_with_another_name_is_rejected(self):
        def call(path, **_kwargs):
            if path.endswith('/myself'): return {'accountId': 'admin-account'}
            if path.endswith('/mypermissions'):
                return {'permissions': {'ADMINISTER': {'havePermission': True}}}
            return {'id': '10001', 'key': 'ALGO', 'name': 'Another Space',
                    'simplified': False}
        with self.assertRaisesRegex(JiraProvisionError, 'another name'):
            ensure_space(call, apply=True)

    def test_full_preparation_keeps_legacy_key_only_behavior(self):
        call, _calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'Existing Name',
            'issueTypes': [], 'simplified': False})
        result = prepare_jira(call, apply=True)
        self.assertTrue(result['ready'])

    def test_apply_creates_project_type_fields_and_screen_without_secrets(self):
        call, calls = self.fixture()
        result = prepare_jira(call, apply=True)
        self.assertTrue(result['ready'])
        self.assertEqual(set(result['fields']), {'structured', 'id', 'status', 'owner'})
        mutations = [(path, options.get('method')) for path, options in calls
                     if options.get('method') in ('POST', 'PUT')]
        self.assertIn(('/rest/api/3/project', 'POST'), mutations)
        self.assertIn(('/rest/api/3/issuetype', 'POST'), mutations)
        self.assertNotIn('token', str(result).lower())

    def test_custom_work_type_name_avoids_global_name_collisions(self):
        base_call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        def call(path, **options):
            if path == '/rest/api/3/issuetype' and options.get('method') != 'POST':
                return [
                    {'id': '10020', 'name': 'Network Access', 'subtask': False},
                    {'id': '10021', 'name': 'Network Access', 'subtask': False},
                ]
            return base_call(path, **options)

        result = prepare_jira(
            call, apply=True, issue_type_name='TESTBUS Network Access')

        created = next(options['body'] for path, options in calls
                       if path == '/rest/api/3/issuetype'
                       and options.get('method') == 'POST')
        self.assertEqual(created['name'], 'TESTBUS Network Access')
        self.assertEqual(result['issue_type_name'], 'TESTBUS Network Access')

    def test_custom_work_type_name_is_validated(self):
        call, _calls = self.fixture()
        for value in ('', ' Network Access', 'Network Access\nInjected', 'x' * 61):
            with self.subTest(value=value), self.assertRaises(JiraProvisionError):
                prepare_jira(call, apply=True, issue_type_name=value)

    def test_dry_run_stops_before_first_mutation(self):
        call, calls = self.fixture()
        result = prepare_jira(call, apply=False)
        self.assertFalse(result['ready'])
        self.assertFalse(any(options.get('method') in ('POST', 'PUT')
                             for _path, options in calls))

    def test_admin_permission_is_required(self):
        def call(path, **_kwargs):
            if path.endswith('/myself'): return {'accountId': 'x'}
            return {'permissions': {'ADMINISTER': {'havePermission': False}}}
        with self.assertRaises(JiraProvisionError):
            prepare_jira(call, apply=True)

    @patch('algosec_jira_bus.jira_provision.prompt',
           side_effect=['admin@example.test', 'token-value'])
    @patch('algosec_jira_bus.jira_provision.request_json_string')
    @patch('algosec_jira_bus.jira_provision.request_json')
    def test_cli_uses_scalar_decoder_for_name_validation(
            self, request_json, request_json_string, _prompt):
        from algosec_jira_bus.jira_provision import main

        def json_call(_config, path, **_kwargs):
            if path.endswith('/myself'):
                return {'accountId': 'admin-account'}
            if path.endswith('/mypermissions'):
                return {'permissions': {'ADMINISTER': {'havePermission': True}}}
            if path.endswith('/project/TESTBUS'):
                raise __import__('urllib.error').error.HTTPError(
                    'https://example.test', 404, 'Not Found', {}, None)
            if path.endswith('/projectvalidate/key'):
                return {'errorMessages': [], 'errors': {}}
            self.fail('Unexpected JSON path: ' + path)

        request_json.side_effect = json_call
        request_json_string.return_value = 'AlgoSec Test Space'
        stopped = main(['--base-url', 'https://jira.example.test', '--space-only',
                        '--space-key', 'TESTBUS',
                        '--space-name', 'AlgoSec Test Space'])
        self.assertEqual(stopped, 2)
        request_json_string.assert_called_once()

    @patch('algosec_jira_bus.jira_provision.prepare_jira')
    @patch('algosec_jira_bus.jira_provision.prompt', side_effect=[
        'admin@example.test', 'token-value', 'CUSTOM', 'Custom Space',
        'Custom Network Request'])
    def test_cli_prompts_for_space_and_work_type_names(self, _prompt, prepare):
        from algosec_jira_bus.jira_provision import main

        prepare.return_value = {'ready': True}
        self.assertEqual(main(['--base-url', 'https://jira.example.test', '--apply']), 0)
        prepare.assert_called_once_with(
            unittest.mock.ANY, apply=True, project_key='CUSTOM',
            project_name='Custom Space', issue_type_name='Custom Network Request')
