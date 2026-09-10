import unittest

from algosec_jira_bus.jira_provision import JiraProvisionError, TEXT_TYPE, prepare_jira


class JiraProvisionTests(unittest.TestCase):
    def fixture(self):
        calls = []
        fields = [
            {'id': 'customfield_10000', 'name': 'Мережеві доступи AlgoSec',
             'schema': {'custom': 'ari:cloud:ecosystem::extension/app/static/algosec-network-access'}},
        ]
        ids = iter(range(20000, 20100))
        def call(path, **kwargs):
            calls.append((path, kwargs))
            if path.endswith('/myself'): return {'accountId': 'admin-account'}
            if path.endswith('/mypermissions'):
                return {'permissions': {'ADMINISTER': {'havePermission': True}}}
            if '/project/ALGO' in path: return None
            if path == '/rest/api/3/project':
                return {'id': '10001', 'key': 'ALGO', 'issueTypes': []}
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
