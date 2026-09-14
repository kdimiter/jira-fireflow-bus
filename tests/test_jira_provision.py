import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from algosec_jira_bus.jira_provision import (
    JiraProvisionError,
    SPACE_TEMPLATE,
    TEXT_TYPE,
    _page_values,
    _read_credentials,
    _structured_forge_field,
    workflow_profile,
    ensure_space,
    prepare_jira,
)


class JiraProvisionTests(unittest.TestCase):
    def test_workflow_profiles_offer_full_and_compact_shapes(self):
        compact = workflow_profile('compact')
        full = workflow_profile('full')
        self.assertEqual([name for name, _category in compact['statuses']],
                         ['To Do', 'In Work', 'Done', 'Rejected / Cancelled'])
        self.assertEqual([name for name, _category in full['statuses']],
                         ['To Do', 'Plan', 'Approve', 'Implement', 'Validate',
                          'Match', 'Done', 'Rejected', 'Cancelled'])
        self.assertNotEqual(compact['workflow_suffix'], full['workflow_suffix'])
        self.assertNotEqual(compact['scheme_suffix'], full['scheme_suffix'])
        with self.assertRaisesRegex(JiraProvisionError, 'workflow profile'):
            workflow_profile('other')

    def test_reads_owner_only_setup_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.chmod(0o700)
            (root / 'email').write_text('admin@example.test')
            (root / 'token').write_text('synthetic-token')
            (root / 'email').chmod(0o600)
            (root / 'token').chmod(0o600)

            self.assertEqual(
                _read_credentials(root),
                ('admin@example.test', 'synthetic-token'))

    def test_rejects_group_readable_setup_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.chmod(0o700)
            (root / 'email').write_text('admin@example.test')
            (root / 'token').write_text('synthetic-token')
            (root / 'email').chmod(0o640)
            (root / 'token').chmod(0o600)

            with self.assertRaisesRegex(JiraProvisionError, 'owner-only'):
                _read_credentials(root)

    def test_structured_field_prefers_the_single_production_installation(self):
        fields = [
            {'id': 'customfield_10110', 'name': 'Мережеві доступи AlgoSec',
             'schema': {
                 'custom': 'ari:cloud:ecosystem::extension/dev/static/algosec-network-access',
                 'configuration': {'environment': 'DEVELOPMENT'}}},
            {'id': 'customfield_10218', 'name': 'Мережеві доступи AlgoSec',
             'schema': {
                 'custom': 'ari:cloud:ecosystem::extension/prod/static/algosec-network-access',
                 'configuration': {'environment': 'PRODUCTION'}}},
        ]

        self.assertEqual(_structured_forge_field(fields)['id'], 'customfield_10218')

    def test_structured_field_rejects_multiple_production_installations(self):
        fields = [
            {'id': str(index), 'name': 'Мережеві доступи AlgoSec',
             'schema': {
                 'custom': f'ari:cloud:ecosystem::extension/{index}/static/algosec-network-access',
                 'configuration': {'environment': 'PRODUCTION'}}}
            for index in (1, 2)
        ]

        with self.assertRaisesRegex(JiraProvisionError, 'More than one production'):
            _structured_forge_field(fields)

    def test_structured_field_selects_requested_production_app(self):
        wanted = '12345678-1234-1234-1234-123456789abc'
        fields = [
            {'id': 'customfield_' + str(index),
             'name': 'Мережеві доступи AlgoSec',
             'schema': {
                 'custom': ('ari:cloud:ecosystem::extension/' + app_id
                            + '/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
                            + '/static/algosec-network-access'),
                 'configuration': {'environment': 'PRODUCTION'}}}
            for index, app_id in enumerate(
                (wanted, 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'), 1)
        ]

        selected = _structured_forge_field(
            fields, 'ari:cloud:ecosystem::app/' + wanted)

        self.assertEqual(selected['id'], 'customfield_1')

    def test_structured_field_rejects_unknown_requested_app(self):
        fields = []
        with self.assertRaisesRegex(JiraProvisionError, 'no unique production field'):
            _structured_forge_field(
                fields,
                'ari:cloud:ecosystem::app/12345678-1234-1234-1234-123456789abc')

    def test_structured_field_rejects_requested_development_copy(self):
        app_id = '12345678-1234-1234-1234-123456789abc'
        fields = [{
            'id': 'customfield_1', 'name': 'Мережеві доступи AlgoSec',
            'schema': {
                'custom': ('ari:cloud:ecosystem::extension/' + app_id
                           + '/static/algosec-network-access'),
                'configuration': {'environment': 'DEVELOPMENT'}},
        }]

        with self.assertRaisesRegex(JiraProvisionError, 'no unique production field'):
            _structured_forge_field(fields, app_id)

    def test_structured_field_rejects_invalid_requested_app_id(self):
        with self.assertRaisesRegex(JiraProvisionError, 'Invalid Forge App ID'):
            _structured_forge_field([], 'not-an-app')

    def fixture(self, initial_project=None):
        calls = []
        project = initial_project
        field_configurations = [
            {'id': '10000', 'name': 'Default Field Configuration',
             'description': '', 'isDefault': True},
        ]
        field_configuration_items = {}
        field_configuration_schemes = []
        field_configuration_mappings = {}
        project_field_scheme = None
        project_issue_type_scheme = '10003'
        issue_type_schemes = [{
            'id': '10003', 'name': 'Default Issue Type Scheme',
            'description': '', 'defaultIssueTypeId': '10000',
            'isDefault': True,
        }]
        issue_type_scheme_items = {'10003': []}
        project_workflow_scheme = '10002'
        workflow_documents = []
        workflow_schemes = [{
            'id': '10002', 'name': 'Default Workflow Scheme',
            'description': '', 'defaultWorkflow': 'jira',
            'issueTypeMappings': {},
        }]
        statuses = [
            {'id': '10040', 'name': 'To Do', 'description': '',
             'scope': {'type': 'GLOBAL'}, 'statusCategory': 'TODO'},
            {'id': '10043', 'name': 'Done', 'description': '',
             'scope': {'type': 'GLOBAL'}, 'statusCategory': 'DONE'},
        ]
        issue_types = [
            {**item, 'hierarchyLevel': item.get(
                'hierarchyLevel', -1 if item.get('subtask') else 0)}
            for item in (initial_project or {}).get('issueTypes', [])]
        fields = [
            {'id': 'customfield_10000', 'name': 'Мережеві доступи AlgoSec',
             'schema': {'custom': 'ari:cloud:ecosystem::extension/app/static/algosec-network-access'}},
        ]
        ids = iter(range(20000, 20100))
        def call(path, **kwargs):
            nonlocal project, project_field_scheme, project_issue_type_scheme
            nonlocal project_workflow_scheme
            calls.append((path, kwargs))
            if path.endswith('/myself'): return {'accountId': 'admin-account'}
            if path.endswith('/mypermissions'):
                return {'permissions': {'ADMINISTER': {'havePermission': True}}}
            if path.endswith('/projectvalidate/key'):
                return {'errorMessages': [], 'errors': {}}
            if path.endswith('/projectvalidate/validProjectName'):
                return kwargs['query']['name']
            if path == '/rest/api/3/project/search':
                values = [] if project is None else [{'id': str(project['id'])}]
                return {'values': values, 'isLast': True, 'startAt': 0,
                        'maxResults': 100, 'total': len(values)}
            if path.startswith('/rest/api/3/project/'): return project
            if path == '/rest/api/3/project':
                project = {'id': '10001', 'key': kwargs['body']['key'],
                           'name': kwargs['body']['name'], 'issueTypes': [],
                           'simplified': False}
                return project
            if path == '/rest/api/3/issuetype' and kwargs.get('method') != 'POST':
                return list(issue_types)
            if path == '/rest/api/3/issuetype':
                item = {'id': str(next(ids)), 'name': kwargs['body']['name'],
                        'hierarchyLevel': 0, 'subtask': False}
                issue_types.append(item)
                issue_type_scheme_items['10003'].append(item['id'])
                return item
            if path == '/rest/api/3/issuetypescheme/project':
                if kwargs.get('method') == 'PUT':
                    project_issue_type_scheme = kwargs['body']['issueTypeSchemeId']
                    scheme_items = issue_type_scheme_items[project_issue_type_scheme]
                    project['issueTypes'] = [
                        item for item in issue_types if item['id'] in scheme_items]
                    return None
                scheme = next(item for item in issue_type_schemes
                              if item['id'] == project_issue_type_scheme)
                return {'values': [{
                    'issueTypeScheme': copy.deepcopy(scheme),
                    'projectIds': [str(project['id'])],
                }], 'isLast': True, 'startAt': 0, 'maxResults': 100, 'total': 1}
            if path == '/rest/api/3/issuetypescheme/mapping':
                requested = kwargs.get('query', {}).get('issueTypeSchemeId')
                scheme_ids = [str(value) for value in requested] if requested else list(
                    issue_type_scheme_items)
                values = [
                    {'issueTypeSchemeId': scheme_id, 'issueTypeId': issue_type_id}
                    for scheme_id in scheme_ids
                    for issue_type_id in issue_type_scheme_items.get(scheme_id, [])]
                return {'values': values, 'isLast': True, 'startAt': 0,
                        'maxResults': 100, 'total': len(values)}
            if path == '/rest/api/3/issuetypescheme' and kwargs.get('method') != 'POST':
                query_string = kwargs.get('query', {}).get('queryString')
                values = [copy.deepcopy(item) for item in issue_type_schemes
                          if not query_string or query_string.casefold() in
                          item['name'].casefold()]
                for item in values:
                    projects = ([{'id': str(project['id'])}]
                                if item['id'] == project_issue_type_scheme else [])
                    item['projects'] = {'values': projects, 'isLast': True,
                                        'startAt': 0, 'maxResults': 100,
                                        'total': len(projects)}
                return {'values': values, 'isLast': True, 'startAt': 0,
                        'maxResults': 100, 'total': len(values)}
            if path == '/rest/api/3/issuetypescheme':
                scheme_id = str(next(ids))
                item = {'id': scheme_id, 'name': kwargs['body']['name'],
                        'description': kwargs['body'].get('description', ''),
                        'defaultIssueTypeId': kwargs['body'].get('defaultIssueTypeId')}
                issue_type_schemes.append(item)
                issue_type_scheme_items[scheme_id] = list(kwargs['body']['issueTypeIds'])
                return {'issueTypeSchemeId': scheme_id}
            if path.endswith('/issuetype') and kwargs.get('method') == 'PUT':
                return None
            if path == '/rest/api/3/search/jql':
                return {'issues': []}
            if path == '/rest/api/3/statuses/byNames':
                names = kwargs.get('query', {}).get('name', [])
                return [copy.deepcopy(item) for item in statuses
                        if item['name'] in names]
            if path == '/rest/api/3/workflows/create/validation':
                return {'errors': []}
            if path == '/rest/api/3/workflows/create':
                document = copy.deepcopy(kwargs['body'])
                for status in document['statuses']:
                    status['scope'] = copy.deepcopy(document['scope'])
                for workflow in document['workflows']:
                    workflow['id'] = 'workflow-' + str(next(ids))
                    workflow['scope'] = copy.deepcopy(document['scope'])
                workflow_documents.append(document)
                return copy.deepcopy(document)
            if path == '/rest/api/3/workflows':
                body = kwargs.get('body', {})
                wanted_names = set(body.get('workflowNames', []))
                wanted_ids = set(body.get('workflowIds', []))
                matched = []
                matched_statuses = []
                for document in workflow_documents:
                    selected = [workflow for workflow in document['workflows']
                                if workflow.get('name') in wanted_names
                                or workflow.get('id') in wanted_ids]
                    if selected:
                        matched.extend(copy.deepcopy(selected))
                        matched_statuses.extend(copy.deepcopy(document['statuses']))
                return {'workflows': matched, 'statuses': matched_statuses}
            if path == '/rest/api/3/workflowscheme/project':
                if kwargs.get('method') == 'PUT':
                    project_workflow_scheme = str(kwargs['body']['workflowSchemeId'])
                    return None
                scheme = next(item for item in workflow_schemes
                              if item['id'] == project_workflow_scheme)
                scheme = copy.deepcopy(scheme)
                if scheme.get('name') == 'Default Workflow Scheme':
                    scheme.pop('id', None)
                return {'values': [{'workflowScheme': scheme}]}
            if (path.startswith('/rest/api/3/workflowscheme/')
                    and path.endswith('/projectUsages')):
                scheme_id = path.split('/')[5]
                values = ([{'id': str(project['id'])}]
                          if project_workflow_scheme == scheme_id else [])
                return {'workflowSchemeId': scheme_id,
                        'projects': {'values': values, 'nextPageToken': None}}
            if path.startswith('/rest/api/3/workflowscheme/'):
                scheme_id = path.split('/')[5]
                return copy.deepcopy(next(item for item in workflow_schemes
                                          if item['id'] == scheme_id))
            if path == '/rest/api/3/workflowscheme' and kwargs.get('method') != 'POST':
                return {'values': copy.deepcopy(workflow_schemes), 'isLast': True,
                        'startAt': 0, 'maxResults': 100,
                        'total': len(workflow_schemes)}
            if path == '/rest/api/3/workflowscheme':
                item = {'id': str(next(ids)), **copy.deepcopy(kwargs['body'])}
                workflow_schemes.append(item)
                return copy.deepcopy(item)
            if path == '/rest/api/3/field' and kwargs.get('method') != 'POST': return fields
            if path == '/rest/api/3/field':
                item = {'id': 'customfield_' + str(next(ids)),
                        'name': kwargs['body']['name'], 'schema': {'custom': TEXT_TYPE}}
                fields.append(item); return item
            if path == '/rest/api/3/fieldconfiguration' and kwargs.get('method') != 'POST':
                return {'values': list(field_configurations), 'isLast': True,
                        'startAt': 0, 'maxResults': 100,
                        'total': len(field_configurations)}
            if path == '/rest/api/3/fieldconfiguration':
                item = {'id': str(next(ids)), 'name': kwargs['body']['name'],
                        'description': kwargs['body'].get('description', ''),
                        'isDefault': False}
                field_configurations.append(item)
                field_configuration_items[item['id']] = [
                    {'id': field['id'], 'isHidden': True, 'isRequired': False}
                    for field in fields]
                return item
            if path.startswith('/rest/api/3/fieldconfiguration/') and path.endswith('/fields'):
                config_id = path.split('/')[5]
                if kwargs.get('method') == 'PUT':
                    current = {item['id']: item for item in
                               field_configuration_items.setdefault(config_id, [])}
                    for update in kwargs['body']['fieldConfigurationItems']:
                        current.setdefault(update['id'], {'id': update['id']}).update(update)
                    field_configuration_items[config_id] = list(current.values())
                    return None
                values = list(field_configuration_items.get(config_id, []))
                return {'values': values, 'isLast': True, 'startAt': 0,
                        'maxResults': 100, 'total': len(values)}
            if path == '/rest/api/3/fieldconfigurationscheme/project':
                if kwargs.get('method') == 'PUT':
                    project_field_scheme = kwargs['body']['fieldConfigurationSchemeId']
                    return None
                values = ([{'projectIds': [str(project['id'])]}]
                          if project_field_scheme is None else [{
                              'fieldConfigurationScheme': {
                                  'id': project_field_scheme},
                              'projectIds': [str(project['id'])]}])
                return {'values': values, 'isLast': True, 'startAt': 0,
                        'maxResults': 100, 'total': len(values)}
            if path == '/rest/api/3/fieldconfigurationscheme/mapping':
                requested = kwargs.get('query', {}).get('fieldConfigurationSchemeId')
                values = (list(field_configuration_mappings.get(str(requested[0]), []))
                          if requested else [item for mappings in
                                             field_configuration_mappings.values()
                                             for item in mappings])
                return {'values': values, 'isLast': True, 'startAt': 0,
                        'maxResults': 100, 'total': len(values)}
            if (path.startswith('/rest/api/3/fieldconfigurationscheme/')
                    and path.endswith('/mapping')):
                scheme_id = path.split('/')[5]
                current = {item['issueTypeId']: item for item in
                           field_configuration_mappings.setdefault(scheme_id, [])}
                for item in kwargs['body']['mappings']:
                    current[item['issueTypeId']] = {
                        **item, 'fieldConfigurationSchemeId': scheme_id}
                field_configuration_mappings[scheme_id] = list(current.values())
                return None
            if path == '/rest/api/3/fieldconfigurationscheme' and kwargs.get('method') != 'POST':
                return {'values': list(field_configuration_schemes), 'isLast': True,
                        'startAt': 0, 'maxResults': 100,
                        'total': len(field_configuration_schemes)}
            if path == '/rest/api/3/fieldconfigurationscheme':
                item = {'id': str(next(ids)), 'name': kwargs['body']['name'],
                        'description': kwargs['body'].get('description', '')}
                field_configuration_schemes.append(item)
                return item
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
        self.assertEqual(
            result['issue_layout'],
            {
                'path': ('/plugins/servlet/project-config/ALGO/issuelayout'
                         '?screenId=' + result['screen_id']),
                'structured_field_section': 'Description fields',
                'result_field_section': 'Context fields',
                'automatic': False,
            })
        self.assertIn('Description fields', result['manual'][0])
        mutations = [(path, options.get('method')) for path, options in calls
                     if options.get('method') in ('POST', 'PUT')]
        self.assertIn(('/rest/api/3/project', 'POST'), mutations)
        self.assertIn(('/rest/api/3/issuetype', 'POST'), mutations)
        self.assertNotIn('token', str(result).lower())

    def test_apply_creates_exact_basic_workflow_and_isolated_mapping(self):
        call, calls = self.fixture()

        result = prepare_jira(call, apply=True, project_key='BASIC',
                              project_name='Basic Requests',
                              issue_type_name='Basic Network Request',
                              workflow_profile_name='compact')

        issue_type_body = next(options['body'] for path, options in calls
                               if path == '/rest/api/3/issuetype'
                               and options.get('method') == 'POST')
        self.assertEqual(issue_type_body['hierarchyLevel'], 0)
        self.assertNotIn('type', issue_type_body)
        work_type_scheme = next(options['body'] for path, options in calls
                                if path == '/rest/api/3/issuetypescheme'
                                and options.get('method') == 'POST')
        self.assertEqual(work_type_scheme['issueTypeIds'],
                         [result['issue_type_id']])
        self.assertEqual(work_type_scheme['defaultIssueTypeId'],
                         result['issue_type_id'])
        work_type_assignment = next(options['body'] for path, options in calls
                                    if path == '/rest/api/3/issuetypescheme/project'
                                    and options.get('method') == 'PUT')
        self.assertEqual(work_type_assignment, {
            'issueTypeSchemeId': result['issue_type_scheme_id'],
            'projectId': '10001',
        })
        payload = next(options['body'] for path, options in calls
                       if path == '/rest/api/3/workflows/create')
        categories = {item['name']: item['statusCategory']
                      for item in payload['statuses']}
        self.assertEqual(categories, {
            'To Do': 'TODO', 'In Work': 'IN_PROGRESS',
            'Done': 'DONE', 'Rejected / Cancelled': 'DONE',
        })
        self.assertNotIn('Approve', categories)
        self.assertNotIn('Implement', categories)
        references = {item['statusReference']: item['name']
                      for item in payload['statuses']}
        transitions = {(item['name'], item['type'],
                        references[item['toStatusReference']])
                       for item in payload['workflows'][0]['transitions']}
        expected = {('Create', 'INITIAL', 'To Do')}
        expected.update((name, 'GLOBAL', name) for name in categories)
        self.assertEqual(transitions, expected)
        scheme_body = next(options['body'] for path, options in calls
                           if path == '/rest/api/3/workflowscheme'
                           and options.get('method') == 'POST')
        self.assertEqual(scheme_body['defaultWorkflow'], 'jira')
        self.assertEqual(scheme_body['issueTypeMappings'], {
            result['issue_type_id']: result['workflow_name']})
        assignment = next(options['body'] for path, options in calls
                          if path == '/rest/api/3/workflowscheme/project'
                          and options.get('method') == 'PUT')
        self.assertEqual(assignment, {
            'projectId': '10001',
            'workflowSchemeId': result['workflow_scheme_id'],
        })

    def test_workflow_preparation_is_idempotent(self):
        call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})
        first = prepare_jira(call, apply=True)
        calls.clear()

        second = prepare_jira(call, apply=True)

        workflow_mutations = [path for path, options in calls
                              if options.get('method') in ('POST', 'PUT')
                              and (path == '/rest/api/3/issuetypescheme'
                                   or path == '/rest/api/3/issuetypescheme/project'
                                   or path.startswith('/rest/api/3/workflows/create')
                                   or path == '/rest/api/3/workflowscheme'
                                   or path == '/rest/api/3/workflowscheme/project')]
        self.assertEqual(workflow_mutations, [])
        self.assertEqual(second['workflow_id'], first['workflow_id'])
        self.assertEqual(second['workflow_scheme_id'],
                         first['workflow_scheme_id'])

    def test_existing_work_type_scheme_requires_complete_project_usage(self):
        base_call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})
        prepare_jira(base_call, apply=True)
        calls.clear()

        def call(path, **options):
            result = base_call(path, **options)
            if (path == '/rest/api/3/issuetypescheme'
                    and options.get('method') != 'POST'):
                result = copy.deepcopy(result)
                for item in result['values']:
                    if item.get('name') == 'ALGO Jira-FireFlow Work Type Scheme':
                        item.pop('projects', None)
            return result

        with self.assertRaisesRegex(JiraProvisionError, 'incomplete work type scheme usage'):
            prepare_jira(call, apply=True)
        self.assertFalse(any(path == '/rest/api/3/issuetypescheme/project'
                             and options.get('method') == 'PUT'
                             for path, options in calls))

    def test_existing_work_type_scheme_with_extra_type_is_refused(self):
        base_call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})
        first = prepare_jira(base_call, apply=True)
        calls.clear()

        def call(path, **options):
            result = base_call(path, **options)
            if path == '/rest/api/3/issuetypescheme/mapping':
                result = copy.deepcopy(result)
                result['values'].append({
                    'issueTypeSchemeId': first['issue_type_scheme_id'],
                    'issueTypeId': '19999',
                })
                result['total'] = len(result['values'])
            return result

        with self.assertRaisesRegex(JiraProvisionError, 'unexpected work types'):
            prepare_jira(call, apply=True)

    def test_nonempty_space_refuses_automatic_workflow_migration(self):
        base_call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        def call(path, **options):
            if path == '/rest/api/3/search/jql':
                return {'issues': [{'id': '10099'}]}
            return base_call(path, **options)

        with self.assertRaisesRegex(JiraProvisionError, 'migration is refused'):
            prepare_jira(call, apply=True)
        self.assertFalse(any(path.startswith('/rest/api/3/workflows/create')
                             for path, _options in calls))

    def test_same_named_unmanaged_workflow_is_refused(self):
        base_call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        def call(path, **options):
            if path == '/rest/api/3/workflows':
                return {'workflows': [{
                    'id': 'foreign-workflow',
                    'name': 'ALGO Jira-FireFlow Basic Workflow',
                    'description': 'Created by somebody else',
                    'scope': {'type': 'GLOBAL'},
                    'statuses': [], 'transitions': [],
                }], 'statuses': []}
            return base_call(path, **options)

        with self.assertRaisesRegex(JiraProvisionError, 'not managed'):
            prepare_jira(call, apply=True)
        self.assertFalse(any(path == '/rest/api/3/workflows/create'
                             for path, _options in calls))

    def test_apply_isolates_required_and_result_fields_from_default_configuration(self):
        call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        result = prepare_jira(call, apply=True)

        configuration = next(options['body'] for path, options in calls
                             if path == '/rest/api/3/fieldconfiguration'
                             and options.get('method') == 'POST')
        self.assertEqual(configuration['name'],
                         'ALGO Jira-FireFlow Field Configuration')
        update_path, update = next((path, options) for path, options in calls
                                   if path.startswith('/rest/api/3/fieldconfiguration/')
                                   and path.endswith('/fields')
                                   and options.get('method') == 'PUT')
        self.assertNotEqual(update_path, '/rest/api/3/fieldconfiguration/10000/fields')
        configured = call(update_path)
        items = {item['id']: item for item in configured['values']}
        self.assertEqual(items[result['fields']['structured']], {
            'id': result['fields']['structured'], 'isHidden': False,
            'isRequired': True})
        for key in ('id', 'status', 'owner'):
            self.assertEqual(items[result['fields'][key]], {
                'id': result['fields'][key], 'isHidden': False,
                'isRequired': False})
        self.assertEqual(result['field_configuration_id'],
                         update_path.split('/')[5])

    def test_field_configuration_scheme_maps_only_selected_work_type(self):
        call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        result = prepare_jira(call, apply=True)

        mapping = next(options['body']['mappings'] for path, options in calls
                       if path.endswith('/mapping')
                       and options.get('method') == 'PUT')
        by_type = {item['issueTypeId']: item['fieldConfigurationId']
                   for item in mapping}
        self.assertEqual(by_type['default'], '10000')
        self.assertEqual(by_type[result['issue_type_id']],
                         result['field_configuration_id'])
        assignment = next(options['body'] for path, options in calls
                          if path == '/rest/api/3/fieldconfigurationscheme/project'
                          and options.get('method') == 'PUT')
        self.assertEqual(assignment, {
            'fieldConfigurationSchemeId': result['field_configuration_scheme_id'],
            'projectId': '10001'})

    def test_field_configuration_setup_is_idempotent(self):
        call, first_calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})
        first = prepare_jira(call, apply=True)
        first_calls.clear()

        second = prepare_jira(call, apply=True)

        field_mutations = [path for path, options in first_calls
                           if options.get('method') in ('POST', 'PUT')
                           and path.startswith('/rest/api/3/fieldconfiguration')]
        self.assertEqual(field_mutations, [])
        self.assertEqual(second['field_configuration_id'],
                         first['field_configuration_id'])
        self.assertEqual(second['field_configuration_scheme_id'],
                         first['field_configuration_scheme_id'])

    def test_existing_project_field_mappings_are_preserved(self):
        base_call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        def call(path, **options):
            if (path == '/rest/api/3/fieldconfigurationscheme/project'
                    and options.get('method') != 'PUT'):
                return {'values': [{
                    'fieldConfigurationScheme': {
                        'id': '19000', 'name': 'Existing project scheme'},
                    'projectIds': ['10001']}], 'isLast': True,
                        'startAt': 0, 'maxResults': 100, 'total': 1}
            if path == '/rest/api/3/fieldconfigurationscheme/mapping':
                requested = options.get('query', {}).get(
                    'fieldConfigurationSchemeId')
                if requested == ['19000']:
                    return {'values': [
                        {'fieldConfigurationSchemeId': '19000',
                         'issueTypeId': 'default',
                         'fieldConfigurationId': '19001'},
                        {'fieldConfigurationSchemeId': '19000',
                         'issueTypeId': '10042',
                         'fieldConfigurationId': '19002'},
                    ], 'isLast': True, 'startAt': 0,
                        'maxResults': 100, 'total': 2}
            return base_call(path, **options)

        result = prepare_jira(call, apply=True)

        mapping = next(options['body']['mappings'] for path, options in calls
                       if path.endswith('/mapping')
                       and options.get('method') == 'PUT')
        by_type = {item['issueTypeId']: item['fieldConfigurationId']
                   for item in mapping}
        self.assertEqual(by_type['default'], '19001')
        self.assertEqual(by_type['10042'], '19002')
        self.assertEqual(by_type[result['issue_type_id']],
                         result['field_configuration_id'])

    def test_same_named_unmanaged_field_configuration_is_refused(self):
        base_call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        def call(path, **options):
            if (path == '/rest/api/3/fieldconfiguration'
                    and options.get('method') != 'POST'):
                return {'values': [
                    {'id': '10000', 'name': 'Default Field Configuration',
                     'description': '', 'isDefault': True},
                    {'id': '19000',
                     'name': 'ALGO Jira-FireFlow Field Configuration',
                     'description': 'Created by somebody else',
                     'isDefault': False},
                ], 'isLast': True, 'startAt': 0,
                    'maxResults': 100, 'total': 2}
            return base_call(path, **options)

        with self.assertRaisesRegex(JiraProvisionError, 'not managed'):
            prepare_jira(call, apply=True)
        self.assertFalse(any(path.startswith('/rest/api/3/fieldconfiguration/19000')
                             and options.get('method') == 'PUT'
                             for path, options in calls))

    def test_managed_scheme_shared_with_another_project_is_refused(self):
        base_call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        def call(path, **options):
            if (path == '/rest/api/3/fieldconfigurationscheme'
                    and options.get('method') != 'POST'):
                return {'values': [{
                    'id': '19000',
                    'name': 'ALGO Jira-FireFlow Field Configuration Scheme',
                    'description': 'Managed by jira-fireflow-bus prepare-jira.sh',
                }], 'isLast': True, 'startAt': 0,
                    'maxResults': 100, 'total': 1}
            if path == '/rest/api/3/project/search':
                return {'values': [
                    {'id': '10001'}, {'id': '10002'}], 'isLast': True,
                    'startAt': 0, 'maxResults': 100, 'total': 2}
            if (path == '/rest/api/3/fieldconfigurationscheme/project'
                    and options.get('method') != 'PUT'):
                return {'values': [
                    {'projectIds': ['10001']},
                    {'fieldConfigurationScheme': {'id': '19000'},
                     'projectIds': ['10002']},
                ], 'isLast': True, 'startAt': 0,
                    'maxResults': 100, 'total': 2}
            return base_call(path, **options)

        with self.assertRaisesRegex(JiraProvisionError, 'another Jira project'):
            prepare_jira(call, apply=True)
        self.assertFalse(any(path == '/rest/api/3/fieldconfigurationscheme/project'
                             and options.get('method') == 'PUT'
                             for path, options in calls))

    def test_managed_field_configuration_shared_by_another_scheme_is_not_mutated(self):
        base_call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        def call(path, **options):
            if (path == '/rest/api/3/fieldconfiguration'
                    and options.get('method') != 'POST'):
                return {'values': [
                    {'id': '10000', 'name': 'Default Field Configuration',
                     'description': '', 'isDefault': True},
                    {'id': '18000',
                     'name': 'ALGO Jira-FireFlow Field Configuration',
                     'description': 'Managed by jira-fireflow-bus prepare-jira.sh',
                     'isDefault': False},
                ], 'isLast': True, 'startAt': 0,
                    'maxResults': 100, 'total': 2}
            if (path == '/rest/api/3/fieldconfigurationscheme'
                    and options.get('method') != 'POST'):
                return {'values': [{
                    'id': '19000',
                    'name': 'ALGO Jira-FireFlow Field Configuration Scheme',
                    'description': 'Managed by jira-fireflow-bus prepare-jira.sh',
                }], 'isLast': True, 'startAt': 0,
                    'maxResults': 100, 'total': 1}
            if path == '/rest/api/3/fieldconfigurationscheme/mapping':
                requested = options.get('query', {}).get(
                    'fieldConfigurationSchemeId')
                values = ([] if requested else [{
                    'fieldConfigurationSchemeId': '17000',
                    'issueTypeId': '10050',
                    'fieldConfigurationId': '18000'}])
                return {'values': values, 'isLast': True, 'startAt': 0,
                        'maxResults': 100, 'total': len(values)}
            return base_call(path, **options)

        with self.assertRaisesRegex(JiraProvisionError, 'another field scheme'):
            prepare_jira(call, apply=True)
        self.assertFalse(any(path == '/rest/api/3/fieldconfiguration/18000/fields'
                             and options.get('method') == 'PUT'
                             for path, options in calls))

    def test_project_field_scheme_is_assigned_after_screen_scheme(self):
        call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        prepare_jira(call, apply=True)

        assignments = [(index, path, options) for index, (path, options)
                       in enumerate(calls) if options.get('method') == 'PUT']
        field_assignment = next(index for index, path, _options in assignments
                                if path == '/rest/api/3/fieldconfigurationscheme/project')
        screen_assignment = next(index for index, path, _options in assignments
                                 if path == '/rest/api/3/issuetypescreenscheme/project')
        self.assertGreater(field_assignment, screen_assignment)

    def test_field_configuration_capability_failure_does_not_assign_project(self):
        base_call, calls = self.fixture({
            'id': '10001', 'key': 'ALGO', 'name': 'AlgoSec',
            'issueTypes': [], 'simplified': False})

        def call(path, **options):
            if path == '/rest/api/3/fieldconfigurationscheme/mapping':
                raise JiraProvisionError('Jira API request failed with HTTP 410')
            return base_call(path, **options)

        with self.assertRaisesRegex(
                JiraProvisionError, 'field-configuration API.*not assigned'):
            prepare_jira(call, apply=True)
        self.assertFalse(any(path == '/rest/api/3/fieldconfigurationscheme/project'
                             and options.get('method') == 'PUT'
                             for path, options in calls))

    def test_pagination_rejects_misaligned_or_boolean_metadata(self):
        for page in (
                {'values': [{'id': '1'}], 'isLast': True,
                 'startAt': 1, 'maxResults': 100, 'total': 1},
                {'values': [{'id': '1'}], 'isLast': True,
                 'startAt': False, 'maxResults': 100, 'total': 1},
                {'values': [{'id': '1'}], 'isLast': True,
                 'startAt': 0, 'maxResults': True, 'total': 1}):
            with self.subTest(page=page), self.assertRaisesRegex(
                    JiraProvisionError, 'pagination metadata'):
                _page_values(lambda _path, **_options: page, '/items')

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
            project_name='Custom Space', issue_type_name='Custom Network Request',
            forge_app_id=None, workflow_profile_name='full')
