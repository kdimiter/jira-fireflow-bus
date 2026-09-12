"""Idempotent Jira Cloud preparation for a dedicated Jira-FireFlow project."""
import base64
import getpass
import json
import os
from pathlib import Path
import re
import stat
import urllib.error
import uuid

from .transport import https_origin, request_json, request_json_string
from .console import ConsoleError, prompt


class JiraProvisionError(ValueError):
    """Safe provisioning failure without remote bodies or credentials."""


TEXT_TYPE = 'com.atlassian.jira.plugin.system.customfieldtypes:textfield'
TEXT_SEARCHER = 'com.atlassian.jira.plugin.system.customfieldtypes:textsearcher'
RESULT_FIELDS = ('FireFlow Request ID', 'FireFlow Status', 'FireFlow Owner')
MARKER = 'Managed by jira-fireflow-bus prepare-jira.sh'
SPACE_TEMPLATE = 'com.atlassian.jira-core-project-templates:jira-core-project-management'
PAGE_SIZE = 100
BASIC_WORKFLOW_STATUSES = (
    ('To Do', 'TODO'),
    ('Plan', 'IN_PROGRESS'),
    ('Approve', 'IN_PROGRESS'),
    ('Implement', 'IN_PROGRESS'),
    ('Validate', 'IN_PROGRESS'),
    ('Match', 'IN_PROGRESS'),
    ('Done', 'DONE'),
    ('Rejected', 'DONE'),
    ('Cancelled', 'DONE'),
)
BASIC_WORKFLOW_TRANSITION_IDS = {
    'To Do': '11', 'Plan': '21', 'Approve': '31', 'Implement': '41',
    'Validate': '51', 'Match': '61', 'Done': '71', 'Rejected': '81',
    'Cancelled': '91',
}


def _identifier(value, label):
    value = str(value)
    if not value.isdecimal():
        raise JiraProvisionError('Jira returned an invalid ' + label)
    return value


def _administrator(call):
    identity = call('/rest/api/3/myself')
    account_id = identity.get('accountId') if isinstance(identity, dict) else None
    if not isinstance(account_id, str) or not account_id:
        raise JiraProvisionError('Jira administrator identity could not be verified')
    permissions = call('/rest/api/3/mypermissions', query={'permissions': 'ADMINISTER'})
    permission = (permissions.get('permissions', {}).get('ADMINISTER', {})
                  if isinstance(permissions, dict) else {})
    if permission.get('havePermission') is not True:
        raise JiraProvisionError('The API account does not have Jira Administrator permission')
    return account_id


def _page_values(call, path, *, query=None):
    """Read a Jira page collection completely and reject malformed pagination."""
    values = []
    start_at = 0
    while True:
        page_query = dict(query or {})
        page_query.update(startAt=start_at, maxResults=PAGE_SIZE)
        page = call(path, query=page_query)
        batch = page.get('values') if isinstance(page, dict) else None
        if not isinstance(batch, list):
            raise JiraProvisionError('Jira returned an invalid paginated response')
        page_start = page.get('startAt')
        page_size = page.get('maxResults')
        if (type(page_start) is not int or page_start != start_at
                or type(page_size) is not int or page_size < 1):
            raise JiraProvisionError('Jira returned invalid pagination metadata')
        values.extend(batch)
        if page.get('isLast') is True:
            return values
        total = page.get('total')
        if type(total) is int and total >= 0 and len(values) >= total:
            return values
        if not batch:
            raise JiraProvisionError('Jira pagination did not advance')
        next_start = page_start + page_size
        if next_start <= start_at:
            raise JiraProvisionError('Jira pagination did not advance')
        start_at = next_start


def _unique_named(items, name, label):
    matches = [item for item in items if item.get('name') == name]
    if len(matches) > 1:
        raise JiraProvisionError('Duplicate managed Jira ' + label + ' found')
    return matches[0] if matches else None


def _managed_named(items, name, label):
    item = _unique_named(items, name, label)
    if item is not None and item.get('description') != MARKER:
        raise JiraProvisionError(
            'Existing Jira ' + label + ' with the managed name is not managed by this installer')
    return item


def _forge_app_uuid(app_id):
    """Return the UUID part of a Forge app ARI and reject unsafe selectors."""
    if app_id is None:
        return None
    prefix = 'ari:cloud:ecosystem::app/'
    value = app_id[len(prefix):] if app_id.startswith(prefix) else app_id
    if not re.fullmatch(
            r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
            r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', value):
        raise JiraProvisionError('Invalid Forge App ID')
    return value.lower()


def _read_credentials(directory):
    """Read root-owned setup credentials from an owner-only bind mount."""
    root = Path(directory)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise JiraProvisionError('Jira credentials directory is unsafe')
    root_stat = root.stat()
    if root_stat.st_uid != os.getuid() or stat.S_IMODE(root_stat.st_mode) != 0o700:
        raise JiraProvisionError('Jira credentials directory must be owner-only (0700)')
    values = []
    for name in ('email', 'token'):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise JiraProvisionError('Jira credential file is missing or unsafe')
        file_stat = path.stat()
        if file_stat.st_uid != os.getuid() or stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise JiraProvisionError('Jira credential files must be owner-only (0600)')
        value = path.read_text()
        if (not value or '\n' in value or '\r' in value
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
            raise JiraProvisionError(
                'Jira credentials must be nonempty single-line values')
        values.append(value)
    return tuple(values)


def _structured_forge_field(fields, app_id=None):
    """Select one production Forge field, optionally for an explicit app."""
    app_uuid = _forge_app_uuid(app_id)
    matches = [
        field for field in fields
        if field.get('name') == 'Мережеві доступи AlgoSec'
        and str(field.get('schema', {}).get('custom', '')).endswith(
            '/static/algosec-network-access')
    ]
    if app_uuid is not None:
        matches = [
            field for field in matches
            if re.search(
                r'::extension/' + re.escape(app_uuid)
                + r'(?:/[0-9a-f-]{36})?/static/algosec-network-access$',
                str(field.get('schema', {}).get('custom', '')).lower())
        ]
    production = [
        field for field in matches
        if field.get('schema', {}).get('configuration', {}).get('environment')
        == 'PRODUCTION'
    ]
    if len(production) == 1:
        return production[0]
    if len(production) > 1:
        raise JiraProvisionError(
            'More than one production Forge field named Мережеві доступи AlgoSec exists')
    if app_uuid is not None:
        raise JiraProvisionError(
            'The selected Forge app has no unique production field in this Jira site')
    if len(matches) == 1:
        return matches[0]
    raise JiraProvisionError(
        'Install the repository Forge app; its structured field was not found uniquely')


def _field_api(call, path, **options):
    """Give deprecated/capability failures one safe operator-facing boundary."""
    try:
        return call(path, **options)
    except JiraProvisionError:
        raise JiraProvisionError(
            'Jira field-configuration API is unavailable or incompatible; '
            'the project field scheme was not assigned') from None


def _scheme_mapping(call, scheme_id):
    items = _page_values(
        call, '/rest/api/3/fieldconfigurationscheme/mapping',
        query={'fieldConfigurationSchemeId': [scheme_id]})
    mapping = {}
    for item in items:
        issue_type_id = item.get('issueTypeId') if isinstance(item, dict) else None
        field_configuration_id = (item.get('fieldConfigurationId')
                                  if isinstance(item, dict) else None)
        if issue_type_id != 'default':
            issue_type_id = _identifier(issue_type_id, 'field scheme work type ID')
        field_configuration_id = _identifier(
            field_configuration_id, 'mapped field configuration ID')
        if issue_type_id in mapping:
            raise JiraProvisionError('Jira field configuration scheme has duplicate mappings')
        mapping[issue_type_id] = field_configuration_id
    return mapping


def _all_project_field_assignments(call):
    projects = _page_values(call, '/rest/api/3/project/search')
    project_ids = [_identifier(item.get('id') if isinstance(item, dict) else None,
                               'project ID') for item in projects]
    assignments = []
    for offset in range(0, len(project_ids), PAGE_SIZE):
        assignments.extend(_page_values(
            call, '/rest/api/3/fieldconfigurationscheme/project',
            query={'projectId': project_ids[offset:offset + PAGE_SIZE]}))
    return assignments


def _assigned_scheme(assignments, project_id):
    matches = []
    for assignment in assignments:
        project_ids = assignment.get('projectIds') if isinstance(assignment, dict) else None
        if not isinstance(project_ids, list):
            raise JiraProvisionError('Jira returned invalid field scheme project assignments')
        if project_id in [str(item) for item in project_ids]:
            matches.append(assignment)
    if len(matches) != 1:
        raise JiraProvisionError('Jira project field scheme was not found uniquely')
    scheme = matches[0].get('fieldConfigurationScheme')
    return (_identifier(scheme.get('id'), 'field configuration scheme ID')
            if isinstance(scheme, dict) else None)


def _ensure_field_configuration(call, *, apply, project_key, project_id,
                                issue_type_id, selected, created):
    """Isolate required/hidden settings in one project field configuration.

    Jira REST v3 contracts:
    https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-field-configurations/
    """
    raw_call = call
    call = lambda path, **options: _field_api(raw_call, path, **options)
    configurations = _page_values(call, '/rest/api/3/fieldconfiguration')
    defaults = [item for item in configurations if item.get('isDefault') is True]
    if len(defaults) != 1:
        raise JiraProvisionError('Jira default field configuration was not found uniquely')
    default_id = _identifier(defaults[0].get('id'), 'default field configuration ID')

    configuration_name = project_key + ' Jira-FireFlow Field Configuration'
    configuration = _managed_named(
        configurations, configuration_name, 'field configuration')
    if configuration is None:
        if not apply:
            return {'ready': False,
                    'planned': ['field-configuration:' + configuration_name]}
        configuration = call('/rest/api/3/fieldconfiguration', method='POST', body={
            'name': configuration_name, 'description': MARKER})
        created.append('field-configuration:' + configuration_name)
    if configuration.get('isDefault') is True:
        raise JiraProvisionError('Managed field configuration cannot be the Jira default')
    configuration_id = _identifier(
        configuration.get('id'), 'field configuration ID')

    scheme_name = project_key + ' Jira-FireFlow Field Configuration Scheme'
    schemes = _page_values(call, '/rest/api/3/fieldconfigurationscheme')
    scheme = _managed_named(schemes, scheme_name, 'field configuration scheme')

    assignments = _all_project_field_assignments(call)
    assigned_scheme_id = _assigned_scheme(assignments, project_id)
    if scheme is not None:
        candidate_scheme_id = _identifier(
            scheme.get('id'), 'field configuration scheme ID')
        foreign_projects = set()
        for assignment in assignments:
            assigned = assignment.get('fieldConfigurationScheme')
            if (not isinstance(assigned, dict)
                    or str(assigned.get('id')) != candidate_scheme_id):
                continue
            foreign_projects.update(
                str(item) for item in assignment.get('projectIds', [])
                if str(item) != project_id)
        if foreign_projects:
            raise JiraProvisionError(
                'Managed Jira field configuration scheme is assigned to another Jira project')

    if assigned_scheme_id is None:
        inherited_mapping = {'default': default_id}
    else:
        inherited_mapping = _scheme_mapping(call, assigned_scheme_id)
        if 'default' not in inherited_mapping:
            raise JiraProvisionError('Current Jira project field scheme has no default mapping')

    if scheme is None:
        if not apply:
            return {'ready': False,
                    'planned': ['field-configuration-scheme:' + scheme_name]}
        scheme = call('/rest/api/3/fieldconfigurationscheme', method='POST', body={
            'name': scheme_name, 'description': MARKER})
        created.append('field-configuration-scheme:' + scheme_name)
    scheme_id = _identifier(scheme.get('id'), 'field configuration scheme ID')

    all_mappings = _page_values(call, '/rest/api/3/fieldconfigurationscheme/mapping')
    foreign_schemes = {
        str(item.get('fieldConfigurationSchemeId')) for item in all_mappings
        if (isinstance(item, dict)
            and str(item.get('fieldConfigurationId')) == configuration_id
            and str(item.get('fieldConfigurationSchemeId')) != scheme_id)
    }
    if foreign_schemes:
        raise JiraProvisionError(
            'Managed Jira field configuration is associated with another field scheme')

    current_items = {item.get('id'): item for item in _page_values(
        call, '/rest/api/3/fieldconfiguration/' + configuration_id + '/fields')}
    desired = {
        selected['structured']: True,
        **{selected[name]: False for name in RESULT_FIELDS},
    }
    updates = []
    for field_id, required in desired.items():
        current = current_items.get(field_id, {})
        if (current.get('isHidden') is not False
                or current.get('isRequired') is not required):
            updates.append({'id': field_id, 'isHidden': False,
                            'isRequired': required})
    if updates:
        if not apply:
            return {'ready': False,
                    'planned': ['field-settings:' + configuration_name]}
        call('/rest/api/3/fieldconfiguration/' + configuration_id + '/fields',
             method='PUT', body={'fieldConfigurationItems': updates})
        created.append('field-settings:' + configuration_name)

    current_mapping = _scheme_mapping(call, scheme_id)
    desired_mapping = dict(current_mapping if assigned_scheme_id == scheme_id
                           else inherited_mapping)
    desired_mapping[issue_type_id] = configuration_id
    if any(current_mapping.get(work_type) != field_configuration
           for work_type, field_configuration in desired_mapping.items()):
        if not apply:
            return {'ready': False, 'planned': ['field-mapping:' + scheme_name]}
        call('/rest/api/3/fieldconfigurationscheme/' + scheme_id + '/mapping',
             method='PUT', body={'mappings': [
                 {'issueTypeId': work_type,
                  'fieldConfigurationId': field_configuration}
                 for work_type, field_configuration in desired_mapping.items()]})
        created.append('field-mapping:' + scheme_name)

    return {'ready': True, 'field_configuration_id': configuration_id,
            'field_configuration_scheme_id': scheme_id,
            'assignment_needed': assigned_scheme_id != scheme_id}


def _assign_field_configuration_scheme(call, *, project_key, project_id,
                                       field_configuration):
    try:
        call('/rest/api/3/fieldconfigurationscheme/project', method='PUT', body={
            'fieldConfigurationSchemeId':
                field_configuration['field_configuration_scheme_id'],
            'projectId': project_id})
    except JiraProvisionError:
        raise JiraProvisionError(
            'Jira field-configuration scheme assignment failed; verify the project '
            'field scheme before rerunning the installer') from None
    return 'field-scheme-project:' + project_key


def _project_issue_type_scheme(call, project_id):
    assignments = _page_values(
        call, '/rest/api/3/issuetypescheme/project',
        query={'projectId': [project_id]})
    matches = [item for item in assignments if isinstance(item, dict)
               and project_id in [str(value) for value in item.get('projectIds', [])]]
    if len(matches) != 1 or not isinstance(matches[0].get('issueTypeScheme'), dict):
        raise JiraProvisionError('Jira project work type scheme was not found uniquely')
    return matches[0]['issueTypeScheme']


def _issue_type_scheme_items(call, scheme_id):
    items = _page_values(
        call, '/rest/api/3/issuetypescheme/mapping',
        query={'issueTypeSchemeId': [scheme_id]})
    result = []
    for item in items:
        if (not isinstance(item, dict)
                or str(item.get('issueTypeSchemeId')) != scheme_id):
            raise JiraProvisionError('Jira returned an invalid work type scheme mapping')
        result.append(_identifier(item.get('issueTypeId'), 'work type ID'))
    if len(result) != len(set(result)):
        raise JiraProvisionError('Jira work type scheme contains duplicate mappings')
    return result


def _ensure_issue_type_scheme(call, *, apply, project_key, project_id,
                              issue_type_id, created):
    """Keep the integration work type in a project-owned scheme."""
    scheme_name = project_key + ' Jira-FireFlow Work Type Scheme'
    current = _project_issue_type_scheme(call, project_id)
    current_id = _identifier(current.get('id'), 'work type scheme ID')
    schemes = _page_values(
        call, '/rest/api/3/issuetypescheme',
        query={'queryString': scheme_name, 'expand': 'projects,issueTypes'})
    scheme = _managed_named(schemes, scheme_name, 'work type scheme')
    candidate_id = (None if scheme is None else
                    _identifier(scheme.get('id'), 'work type scheme ID'))
    if current_id != candidate_id and not _project_is_empty(call, project_key):
        raise JiraProvisionError(
            'Jira Space already contains issues; automatic work type migration is refused')
    if scheme is None:
        if not apply:
            return {'ready': False, 'planned': ['work-type-scheme:' + scheme_name]}
        response = call('/rest/api/3/issuetypescheme', method='POST', body={
            'name': scheme_name, 'description': MARKER,
            'defaultIssueTypeId': issue_type_id,
            'issueTypeIds': [issue_type_id],
        })
        scheme_id = _identifier(
            response.get('issueTypeSchemeId') if isinstance(response, dict) else None,
            'work type scheme ID')
        created.append('work-type-scheme:' + scheme_name)
    else:
        scheme_id = _identifier(scheme.get('id'), 'work type scheme ID')
        projects = scheme.get('projects')
        values = projects.get('values') if isinstance(projects, dict) else None
        if (not isinstance(values, list) or projects.get('isLast') is not True):
            raise JiraProvisionError('Jira returned an incomplete work type scheme usage')
        project_ids = []
        for item in values:
            if not isinstance(item, dict):
                raise JiraProvisionError('Jira returned an invalid work type scheme usage')
            project_ids.append(_identifier(item.get('id'), 'project ID'))
        foreign = {value for value in project_ids if value != project_id}
        if foreign:
            raise JiraProvisionError(
                'Managed Jira work type scheme is assigned to another Jira project')
    if _issue_type_scheme_items(call, scheme_id) != [issue_type_id]:
        raise JiraProvisionError('Managed Jira work type scheme has unexpected work types')
    return {'ready': True, 'issue_type_scheme_id': scheme_id,
            'assignment_needed': current_id != scheme_id}


def _assign_issue_type_scheme(call, *, project_key, project_id, scheme):
    try:
        call('/rest/api/3/issuetypescheme/project', method='PUT', body={
            'issueTypeSchemeId': scheme['issue_type_scheme_id'],
            'projectId': project_id,
        })
    except JiraProvisionError:
        raise JiraProvisionError(
            'Jira work type scheme assignment failed; the Space must be empty') from None
    return 'work-type-scheme-project:' + project_key


def _workflow_id(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 255
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise JiraProvisionError('Jira returned an invalid workflow ID')
    return value


def _verify_basic_workflow(document, workflow_name):
    if not isinstance(document, dict):
        raise JiraProvisionError('Jira returned an invalid workflow response')
    workflows = document.get('workflows')
    statuses = document.get('statuses')
    if not isinstance(workflows, list) or not isinstance(statuses, list):
        raise JiraProvisionError('Jira returned an invalid workflow response')
    matches = [item for item in workflows
               if isinstance(item, dict) and item.get('name') == workflow_name]
    if len(matches) != 1:
        raise JiraProvisionError('Managed Jira workflow was not found uniquely')
    workflow = matches[0]
    if (workflow.get('description') != MARKER
            or workflow.get('scope', {}).get('type') != 'GLOBAL'):
        raise JiraProvisionError(
            'Existing Jira workflow with the managed name is not managed by this installer')

    by_reference = {}
    for item in statuses:
        if not isinstance(item, dict):
            raise JiraProvisionError('Jira returned an invalid workflow status')
        reference = str(item.get('statusReference', ''))
        if not reference or reference in by_reference:
            raise JiraProvisionError('Jira returned duplicate workflow status references')
        by_reference[reference] = item

    used = workflow.get('statuses')
    transitions = workflow.get('transitions')
    if not isinstance(used, list) or not isinstance(transitions, list):
        raise JiraProvisionError('Jira returned an invalid workflow definition')
    used_names = set()
    for item in used:
        if (not isinstance(item, dict) or item.get('properties') not in (None, {})
                or item.get('approvalConfiguration') is not None):
            raise JiraProvisionError('Managed Jira workflow has unexpected status rules')
        status = by_reference.get(str(item.get('statusReference', '')))
        if status is None:
            raise JiraProvisionError('Jira workflow references an unknown status')
        if status.get('scope', {}).get('type') != 'GLOBAL':
            raise JiraProvisionError('Managed Jira workflow uses a non-global status')
        used_names.add(status.get('name'))
    expected_categories = dict(BASIC_WORKFLOW_STATUSES)
    if len(used) != len(expected_categories) or used_names != set(expected_categories):
        raise JiraProvisionError('Managed Jira workflow has unexpected statuses')
    for status in by_reference.values():
        name = status.get('name')
        if name in used_names and status.get('statusCategory') != expected_categories[name]:
            raise JiraProvisionError('Managed Jira workflow has an invalid status category')

    actual_transitions = set()
    for transition in transitions:
        if not isinstance(transition, dict):
            raise JiraProvisionError('Jira returned an invalid workflow transition')
        target = by_reference.get(str(transition.get('toStatusReference', '')))
        if target is None:
            raise JiraProvisionError('Jira workflow transition has an unknown target')
        actual_transitions.add((transition.get('name'), transition.get('type'),
                                target.get('name')))
        for name in ('actions', 'links', 'triggers', 'validators'):
            if transition.get(name) not in (None, []):
                raise JiraProvisionError(
                    'Managed Jira workflow has unexpected transition rules')
        if (transition.get('conditions') not in (None, {})
                or transition.get('transitionScreen') is not None
                or transition.get('properties') not in (None, {})
                or transition.get('customIssueEventId') is not None):
            raise JiraProvisionError(
                'Managed Jira workflow has unexpected transition rules')
    expected_transitions = {('Create', 'INITIAL', 'To Do')}
    expected_transitions.update((name, 'GLOBAL', name)
                                for name, _category in BASIC_WORKFLOW_STATUSES)
    if (len(transitions) != len(expected_transitions)
            or actual_transitions != expected_transitions):
        raise JiraProvisionError('Managed Jira workflow has unexpected transitions')
    return _workflow_id(workflow.get('id'))


def _basic_workflow_payload(call, project_key, workflow_name):
    names = [name for name, _category in BASIC_WORKFLOW_STATUSES]
    existing = call('/rest/api/3/statuses/byNames', query={'name': names})
    if not isinstance(existing, list):
        raise JiraProvisionError('Jira returned an invalid status-name response')
    global_statuses = {}
    for name, category in BASIC_WORKFLOW_STATUSES:
        matches = [item for item in existing if isinstance(item, dict)
                   and item.get('name') == name
                   and item.get('scope', {}).get('type') == 'GLOBAL']
        if len(matches) > 1:
            raise JiraProvisionError('Jira returned duplicate global status: ' + name)
        if matches and matches[0].get('statusCategory') != category:
            raise JiraProvisionError('Existing Jira status has the wrong category: ' + name)
        global_statuses[name] = matches[0] if matches else None

    references = {
        name: str(uuid.uuid5(uuid.NAMESPACE_URL,
                             'jira-fireflow-bus/%s/%s' % (project_key, name)))
        for name in names
    }
    statuses = []
    for name, category in BASIC_WORKFLOW_STATUSES:
        status = {
            'name': name, 'description': MARKER, 'statusCategory': category,
            'statusReference': references[name],
        }
        current = global_statuses[name]
        if current is not None:
            status['id'] = _identifier(current.get('id'), 'status ID')
            status['description'] = str(current.get('description') or '')
        statuses.append(status)

    positions = {name: index for index, name in enumerate(names)}
    workflow_statuses = [
        {'layout': {'x': 120 + positions[name] * 180, 'y': 0},
         'properties': {}, 'statusReference': references[name]}
        for name in names
    ]
    transitions = [{
        'actions': [], 'description': '', 'id': '1', 'links': [],
        'name': 'Create', 'properties': {},
        'toStatusReference': references['To Do'], 'triggers': [],
        'type': 'INITIAL', 'validators': [],
    }]
    transitions.extend({
        'actions': [], 'description': '',
        'id': BASIC_WORKFLOW_TRANSITION_IDS[name], 'links': [],
        'name': name, 'properties': {}, 'toStatusReference': references[name],
        'triggers': [], 'type': 'GLOBAL', 'validators': [],
    } for name in names)
    return {
        'scope': {'type': 'GLOBAL'},
        'statuses': statuses,
        'workflows': [{
            'description': MARKER, 'name': workflow_name,
            'startPointLayout': {'x': -100, 'y': 0},
            'statuses': workflow_statuses, 'transitions': transitions,
        }],
    }


def _project_workflow_scheme(call, project_id):
    response = call('/rest/api/3/workflowscheme/project',
                    query={'projectId': project_id})
    values = response.get('values') if isinstance(response, dict) else None
    if not isinstance(values, list) or len(values) != 1:
        raise JiraProvisionError('Jira project workflow scheme was not found uniquely')
    scheme = values[0].get('workflowScheme')
    if not isinstance(scheme, dict):
        raise JiraProvisionError('Jira returned an invalid project workflow scheme')
    return scheme


def _project_is_empty(call, project_key):
    result = call('/rest/api/3/search/jql', query={
        'jql': 'project = "%s"' % project_key, 'maxResults': 1, 'fields': ['id']})
    issues = result.get('issues') if isinstance(result, dict) else None
    if not isinstance(issues, list):
        raise JiraProvisionError('Jira returned an invalid project issue search response')
    return not issues


def _ensure_basic_workflow(call, *, apply, project_key, project_id,
                           issue_type_id, created):
    """Create and isolate the Basic Change Traffic Request mirror workflow."""
    workflow_name = project_key + ' Jira-FireFlow Basic Workflow'
    scheme_name = project_key + ' Jira-FireFlow Workflow Scheme'
    current_scheme = _project_workflow_scheme(call, project_id)
    current_scheme_id = (None if current_scheme.get('id') is None else
                         _identifier(current_scheme.get('id'), 'workflow scheme ID'))
    current_is_managed = (current_scheme.get('name') == scheme_name
                          and current_scheme.get('description') == MARKER)
    if not current_is_managed and not _project_is_empty(call, project_key):
        raise JiraProvisionError(
            'Jira Space already contains issues; automatic workflow migration is refused')

    read = call('/rest/api/3/workflows', method='POST', optional=True, body={
        'projectAndIssueTypes': [], 'workflowIds': [],
        'workflowNames': [workflow_name],
    })
    if read is None:
        read = {'workflows': [], 'statuses': []}
    workflows = read.get('workflows') if isinstance(read, dict) else None
    if not isinstance(workflows, list):
        raise JiraProvisionError('Jira returned an invalid workflow response')
    named = [item for item in workflows
             if isinstance(item, dict) and item.get('name') == workflow_name]
    if len(named) > 1:
        raise JiraProvisionError('Managed Jira workflow was not found uniquely')
    if named:
        workflow_id = _verify_basic_workflow(read, workflow_name)
    elif not apply:
        return {'ready': False, 'planned': ['workflow:' + workflow_name]}
    else:
        payload = _basic_workflow_payload(call, project_key, workflow_name)
        validation = call('/rest/api/3/workflows/create/validation', method='POST', body={
            'payload': payload, 'validationOptions': {'levels': ['ERROR', 'WARNING']},
        })
        errors = validation.get('errors') if isinstance(validation, dict) else None
        if not isinstance(errors, list):
            raise JiraProvisionError('Jira returned an invalid workflow validation response')
        if any(not isinstance(item, dict)
               or item.get('level') not in ('ERROR', 'WARNING') for item in errors):
            raise JiraProvisionError('Jira returned an invalid workflow validation result')
        if any(item.get('level') == 'ERROR' for item in errors):
            raise JiraProvisionError('Jira rejected the Basic workflow definition')
        created_workflow = call('/rest/api/3/workflows/create', method='POST', body=payload)
        workflow_id = _verify_basic_workflow(created_workflow, workflow_name)
        created.append('workflow:' + workflow_name)

    schemes = _page_values(call, '/rest/api/3/workflowscheme')
    scheme = _managed_named(schemes, scheme_name, 'workflow scheme')
    if scheme is None:
        if not apply:
            return {'ready': False, 'planned': ['workflow-scheme:' + scheme_name]}
        default_workflow = current_scheme.get('defaultWorkflow')
        mappings = current_scheme.get('issueTypeMappings', {})
        if default_workflow is not None and (not isinstance(default_workflow, str)
                                             or not default_workflow):
            raise JiraProvisionError('Jira returned an invalid current workflow scheme')
        if not isinstance(mappings, dict):
            raise JiraProvisionError('Jira returned an invalid current workflow scheme')
        desired_mappings = {str(key): value for key, value in mappings.items()}
        desired_mappings[issue_type_id] = workflow_name
        body = {
            'name': scheme_name, 'description': MARKER,
            'issueTypeMappings': desired_mappings,
        }
        if default_workflow is not None:
            body['defaultWorkflow'] = default_workflow
        scheme = call('/rest/api/3/workflowscheme', method='POST', body=body)
        created.append('workflow-scheme:' + scheme_name)
    scheme_id = _identifier(scheme.get('id'), 'workflow scheme ID')
    scheme = call('/rest/api/3/workflowscheme/' + scheme_id)
    if not isinstance(scheme, dict):
        raise JiraProvisionError('Jira returned an invalid workflow scheme')
    mappings = scheme.get('issueTypeMappings')
    if (not isinstance(mappings, dict)
            or mappings.get(issue_type_id) != workflow_name):
        raise JiraProvisionError('Managed Jira workflow scheme has an invalid work type mapping')
    usages = call('/rest/api/3/workflowscheme/' + scheme_id + '/projectUsages',
                  query={'maxResults': 100})
    projects = usages.get('projects') if isinstance(usages, dict) else None
    values = projects.get('values') if isinstance(projects, dict) else None
    if (str(usages.get('workflowSchemeId')) != scheme_id
            or not isinstance(values, list)
            or projects.get('nextPageToken') not in (None, '')):
        raise JiraProvisionError('Jira returned an invalid workflow scheme usage response')
    project_ids = []
    for item in values:
        if not isinstance(item, dict):
            raise JiraProvisionError('Jira returned an invalid workflow scheme usage')
        project_ids.append(_identifier(item.get('id'), 'project ID'))
    foreign_projects = {value for value in project_ids if value != project_id}
    if foreign_projects:
        raise JiraProvisionError(
            'Managed Jira workflow scheme is assigned to another Jira project')
    return {
        'ready': True, 'workflow_id': workflow_id, 'workflow_name': workflow_name,
        'workflow_scheme_id': scheme_id,
        'assignment_needed': current_scheme_id != scheme_id,
    }


def _assign_workflow_scheme(call, *, project_key, project_id, workflow):
    try:
        call('/rest/api/3/workflowscheme/project', method='PUT', body={
            'projectId': project_id,
            'workflowSchemeId': workflow['workflow_scheme_id'],
        })
    except JiraProvisionError:
        raise JiraProvisionError(
            'Jira workflow scheme assignment failed; the Space must be empty') from None
    return 'workflow-scheme-project:' + project_key


def ensure_space(call, *, apply, project_key='ALGO', project_name='AlgoSec',
                 account_id=None):
    """Create or verify one company-managed Jira Space through the supported project API."""
    if (type(apply) is not bool or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,9}', project_key)
            or not isinstance(project_name, str) or not 1 <= len(project_name.strip()) <= 80
            or project_name != project_name.strip()
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in project_name)):
        raise JiraProvisionError('Invalid Jira Space name, key, or apply option')
    account_id = account_id or _administrator(call)
    project = call('/rest/api/3/project/' + project_key, optional=True)
    if project is not None:
        if (not isinstance(project, dict) or str(project.get('key')) != project_key
                or project.get('name') != project_name):
            raise JiraProvisionError('The Jira Space key already belongs to another name')
        if project.get('simplified') is not False:
            raise JiraProvisionError('Use a company-managed Jira Space')
        return {'ready': True, 'created': False, 'space_key': project_key,
                'space_name': project_name,
                'project_id': _identifier(project.get('id'), 'project ID')}

    key_check = call('/rest/api/3/projectvalidate/key', query={'key': project_key})
    if (not isinstance(key_check, dict) or key_check.get('errorMessages')
            or key_check.get('errors')):
        raise JiraProvisionError('Jira rejected the Space key')
    valid_name = call('/rest/api/3/projectvalidate/validProjectName',
                      query={'name': project_name})
    if valid_name != project_name:
        raise JiraProvisionError('The Jira Space name is unavailable')
    if not apply:
        return {'ready': False, 'created': False, 'space_key': project_key,
                'space_name': project_name, 'planned': ['space:' + project_key]}

    project = call('/rest/api/3/project', method='POST', body={
        'key': project_key, 'name': project_name, 'leadAccountId': account_id,
        'projectTypeKey': 'business', 'projectTemplateKey': SPACE_TEMPLATE,
        'assigneeType': 'PROJECT_LEAD', 'description': MARKER})
    if (not isinstance(project, dict) or str(project.get('key')) != project_key):
        raise JiraProvisionError('Jira did not confirm the created Space')
    return {'ready': True, 'created': True, 'space_key': project_key,
            'space_name': project_name,
            'project_id': _identifier(project.get('id'), 'project ID')}


def prepare_jira(call, *, apply, project_key='ALGO', project_name='AlgoSec',
                 issue_type_name='Network Access', forge_app_id=None):
    """Prepare project, work type, fields and project screen using an admin caller."""
    if (type(apply) is not bool
            or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,9}', project_key)
            or not isinstance(issue_type_name, str)
            or not 1 <= len(issue_type_name) <= 60
            or issue_type_name != issue_type_name.strip()
            or any(ord(char) < 32 or 127 <= ord(char) <= 159
                   for char in issue_type_name)):
        raise JiraProvisionError('Invalid Jira preparation options')
    _forge_app_uuid(forge_app_id)
    account_id = _administrator(call)

    created = []
    project = call('/rest/api/3/project/' + project_key, optional=True)
    if project is None:
        space = ensure_space(call, apply=apply, project_key=project_key,
                             project_name=project_name, account_id=account_id)
        if not space['ready']:
            return {'ready': False, 'planned': ['project:' + project_key],
                    'manual': ['Install the Forge app before apply']}
        project = call('/rest/api/3/project/' + project_key)
        created.append('project:' + project_key)
    project_id = _identifier(project.get('id'), 'project ID')
    if project.get('simplified') is not False:
        raise JiraProvisionError('Use a company-managed Jira project')

    issue_types = call('/rest/api/3/issuetype')
    named_issue_types = [item for item in issue_types
                         if item.get('name') == issue_type_name]
    matches = [item for item in named_issue_types
               if item.get('hierarchyLevel') == 0 and item.get('subtask') is not True]
    if named_issue_types and len(matches) != 1:
        raise JiraProvisionError(
            'Existing Jira work type with this name is ambiguous or not a standard type')
    if not matches:
        if not apply:
            return {'ready': False, 'planned': created + ['issue-type:' + issue_type_name],
                    'manual': ['Install the Forge app before apply']}
        issue_type = call('/rest/api/3/issuetype', method='POST', body={
            'name': issue_type_name, 'description': MARKER, 'hierarchyLevel': 0})
        created.append('issue-type:' + issue_type_name)
    else:
        issue_type = matches[0]
    issue_type_id = _identifier(issue_type.get('id'), 'issue type ID')
    issue_type_scheme = _ensure_issue_type_scheme(
        call, apply=apply, project_key=project_key, project_id=project_id,
        issue_type_id=issue_type_id, created=created)
    if not issue_type_scheme['ready']:
        return {'ready': False,
                'planned': created + issue_type_scheme['planned'], 'manual': []}
    if issue_type_scheme['assignment_needed']:
        if not apply:
            return {'ready': False,
                    'planned': created + ['work-type-scheme-project:' + project_key],
                    'manual': []}
        created.append(_assign_issue_type_scheme(
            call, project_key=project_key, project_id=project_id,
            scheme=issue_type_scheme))
    assigned_issue_type_scheme = _project_issue_type_scheme(call, project_id)
    if str(assigned_issue_type_scheme.get('id')) != issue_type_scheme['issue_type_scheme_id']:
        raise JiraProvisionError('Jira did not confirm the assigned work type scheme')

    workflow = _ensure_basic_workflow(
        call, apply=apply, project_key=project_key, project_id=project_id,
        issue_type_id=issue_type_id, created=created)
    if not workflow['ready']:
        return {'ready': False, 'planned': created + workflow['planned'], 'manual': []}

    fields = call('/rest/api/3/field')
    selected = {
        'structured': _structured_forge_field(fields, forge_app_id)['id']}
    for name in RESULT_FIELDS:
        same_name = [field for field in fields if field.get('name') == name]
        valid = [field for field in same_name
                 if field.get('schema', {}).get('custom') == TEXT_TYPE]
        if same_name and len(valid) != 1:
            raise JiraProvisionError('Existing Jira field has the wrong type or duplicate name: ' + name)
        if valid:
            field = valid[0]
        elif apply:
            field = call('/rest/api/3/field', method='POST', body={
                'name': name, 'description': MARKER, 'type': TEXT_TYPE,
                'searcherKey': TEXT_SEARCHER})
            created.append('field:' + name)
        else:
            return {'ready': False, 'planned': created + ['field:' + name], 'manual': []}
        selected[name] = field['id']

    field_configuration = _ensure_field_configuration(
        call, apply=apply, project_key=project_key, project_id=project_id,
        issue_type_id=issue_type_id, selected=selected, created=created)
    if not field_configuration['ready']:
        return {'ready': False,
                'planned': created + field_configuration['planned'], 'manual': []}

    screen_name = project_key + ' Jira-FireFlow Screen'
    screens = call('/rest/api/3/screens', query={'queryString': screen_name,
                                                 'maxResults': 100})
    screen_matches = [item for item in screens.get('values', [])
                      if item.get('name') == screen_name]
    if len(screen_matches) > 1:
        raise JiraProvisionError('Duplicate managed Jira screens found')
    if not screen_matches:
        if not apply:
            return {'ready': False, 'planned': created + ['screen:' + screen_name],
                    'manual': []}
        screen = call('/rest/api/3/screens', method='POST',
                      body={'name': screen_name, 'description': MARKER})
        created.append('screen:' + screen_name)
    else:
        screen = screen_matches[0]
    screen_id = _identifier(screen.get('id'), 'screen ID')
    tabs = call('/rest/api/3/screens/' + screen_id + '/tabs')
    if not isinstance(tabs, list) or not tabs:
        raise JiraProvisionError('Managed Jira screen has no tab')
    tab_id = _identifier(tabs[0].get('id'), 'screen tab ID')
    present = {item.get('id') for item in call(
        '/rest/api/3/screens/' + screen_id + '/tabs/' + tab_id + '/fields')}
    for field_id in ('summary', 'description', selected['structured'],
                     *(selected[name] for name in RESULT_FIELDS)):
        if field_id not in present and apply:
            call('/rest/api/3/screens/' + screen_id + '/tabs/' + tab_id + '/fields',
                 method='POST', body={'fieldId': field_id})

    screen_scheme_name = project_key + ' Jira-FireFlow Screen Scheme'
    page = call('/rest/api/3/screenscheme',
                query={'queryString': screen_scheme_name, 'maxResults': 100})
    matches = [item for item in page.get('values', [])
               if item.get('name') == screen_scheme_name]
    if len(matches) > 1:
        raise JiraProvisionError('Duplicate managed Jira screen schemes found')
    if matches:
        screen_scheme = matches[0]
    elif apply:
        screen_scheme = call('/rest/api/3/screenscheme', method='POST', body={
            'name': screen_scheme_name, 'description': MARKER,
            'screens': {'default': int(screen_id), 'create': int(screen_id),
                        'edit': int(screen_id), 'view': int(screen_id)}})
        created.append('screen-scheme:' + screen_scheme_name)
    else:
        return {'ready': False, 'planned': created + ['screen-scheme:' + screen_scheme_name],
                'manual': []}
    screen_scheme_id = _identifier(screen_scheme.get('id'), 'screen scheme ID')

    type_screen_name = project_key + ' Jira-FireFlow Issue Type Screen Scheme'
    page = call('/rest/api/3/issuetypescreenscheme',
                query={'queryString': type_screen_name, 'maxResults': 100})
    matches = [item for item in page.get('values', [])
               if item.get('name') == type_screen_name]
    if len(matches) > 1:
        raise JiraProvisionError('Duplicate managed issue type screen schemes found')
    if matches:
        type_screen_scheme = matches[0]
    elif apply:
        type_screen_scheme = call('/rest/api/3/issuetypescreenscheme', method='POST', body={
            'name': type_screen_name, 'description': MARKER,
            'issueTypeMappings': [
                {'issueTypeId': 'default', 'screenSchemeId': screen_scheme_id},
                {'issueTypeId': issue_type_id, 'screenSchemeId': screen_scheme_id}]})
        created.append('issue-type-screen-scheme:' + type_screen_name)
    else:
        return {'ready': False,
                'planned': created + ['issue-type-screen-scheme:' + type_screen_name],
                'manual': []}
    type_screen_scheme_id = _identifier(
        type_screen_scheme.get('id'), 'issue type screen scheme ID')
    if apply:
        call('/rest/api/3/issuetypescreenscheme/project', method='PUT', body={
            'issueTypeScreenSchemeId': type_screen_scheme_id,
            'projectId': project_id})

    if field_configuration['assignment_needed']:
        if not apply:
            return {'ready': False,
                    'planned': created + ['field-scheme-project:' + project_key],
                    'manual': []}
        created.append(_assign_field_configuration_scheme(
            call, project_key=project_key, project_id=project_id,
            field_configuration=field_configuration))

    if workflow['assignment_needed']:
        if not apply:
            return {'ready': False,
                    'planned': created + ['workflow-scheme-project:' + project_key],
                    'manual': []}
        created.append(_assign_workflow_scheme(
            call, project_key=project_key, project_id=project_id, workflow=workflow))

    assigned_workflow_scheme = _project_workflow_scheme(call, project_id)
    if str(assigned_workflow_scheme.get('id')) != workflow['workflow_scheme_id']:
        raise JiraProvisionError('Jira did not confirm the assigned workflow scheme')
    verified_workflow = call('/rest/api/3/workflows', method='POST', body={
        'projectAndIssueTypes': [], 'workflowIds': [workflow['workflow_id']],
        'workflowNames': [],
    })
    _verify_basic_workflow(verified_workflow, workflow['workflow_name'])

    return {
        'ready': bool(apply), 'project_key': project_key, 'project_id': project_id,
        'issue_type_id': issue_type_id, 'issue_type_name': issue_type_name,
        'issue_type_scheme_id': issue_type_scheme['issue_type_scheme_id'],
        'fields': {'structured': selected['structured'],
                   'id': selected['FireFlow Request ID'],
                   'status': selected['FireFlow Status'],
                   'owner': selected['FireFlow Owner']},
        'screen_id': screen_id, 'screen_scheme_id': screen_scheme_id,
        'issue_type_screen_scheme_id': type_screen_scheme_id, 'created': created,
        'field_configuration_id': field_configuration['field_configuration_id'],
        'field_configuration_scheme_id':
            field_configuration['field_configuration_scheme_id'],
        'workflow_id': workflow['workflow_id'],
        'workflow_name': workflow['workflow_name'],
        'workflow_scheme_id': workflow['workflow_scheme_id'],
        'manual': [],
    }


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Prepare Jira Cloud for Jira-FireFlow')
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--project-key', '--space-key', dest='project_key')
    parser.add_argument('--project-name', '--space-name', dest='project_name')
    parser.add_argument('--work-type-name')
    parser.add_argument('--forge-app-id',
                        help='select the installed Forge app ARI when more than one exists')
    parser.add_argument('--credentials-dir',
                        help=argparse.SUPPRESS)
    parser.add_argument('--space-only', action='store_true',
                        help='create or verify only the company-managed Jira Space')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    base_url = https_origin(args.base_url)
    if args.credentials_dir:
        email, token = _read_credentials(args.credentials_dir)
    else:
        email = prompt('Jira administrator email: ').strip()
        token = prompt('Jira administrator API token: ', secret=True)
    if not email or not token or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in email + token):
        raise JiraProvisionError('Jira credentials must be nonempty single-line values')
    project_key = (args.project_key if args.project_key is not None
                   else prompt('Jira Space key [ALGO]: ').strip() or 'ALGO')
    project_name = (args.project_name if args.project_name is not None
                    else prompt('Jira Space name [AlgoSec]: ').strip() or 'AlgoSec')
    issue_type_name = None
    if not args.space_only:
        issue_type_name = (args.work_type_name if args.work_type_name is not None
                           else prompt('Jira work type name [Network Access]: ').strip()
                           or 'Network Access')
    authorization = base64.b64encode((email + ':' + token).encode()).decode('ascii')
    headers = {'Authorization': 'Basic ' + authorization}

    def call(path, *, optional=False, **kwargs):
        try:
            requester = (request_json_string
                         if path.endswith('/projectvalidate/validProjectName')
                         else request_json)
            return requester({'base_url': base_url}, path, headers=headers,
                             timeout=30, **kwargs)
        except urllib.error.HTTPError as error:
            if optional and error.code == 404:
                return None
            raise JiraProvisionError('Jira API request failed with HTTP ' + str(error.code)) from None
        except ValueError:
            raise JiraProvisionError('Jira API returned an invalid response') from None

    if args.space_only:
        result = ensure_space(call, apply=args.apply,
                              project_key=project_key, project_name=project_name)
    else:
        result = prepare_jira(call, apply=args.apply,
                              project_key=project_key, project_name=project_name,
                              issue_type_name=issue_type_name,
                              forge_app_id=args.forge_app_id)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get('ready') else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (JiraProvisionError, ConsoleError) as error:
        print('Jira preparation stopped:', str(error))
        raise SystemExit(1)
