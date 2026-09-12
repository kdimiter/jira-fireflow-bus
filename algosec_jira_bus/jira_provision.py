"""Idempotent Jira Cloud preparation for a dedicated Jira-FireFlow project."""
import base64
import getpass
import json
import re
import urllib.error

from .transport import https_origin, request_json, request_json_string
from .console import ConsoleError, prompt


class JiraProvisionError(ValueError):
    """Safe provisioning failure without remote bodies or credentials."""


TEXT_TYPE = 'com.atlassian.jira.plugin.system.customfieldtypes:textfield'
TEXT_SEARCHER = 'com.atlassian.jira.plugin.system.customfieldtypes:textsearcher'
RESULT_FIELDS = ('FireFlow Request ID', 'FireFlow Status', 'FireFlow Owner')
MARKER = 'Managed by jira-fireflow-bus prepare-jira.sh'
SPACE_TEMPLATE = 'com.atlassian.jira-core-project-templates:jira-core-project-management'


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
        if project.get('simplified') is True:
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
                 issue_type_name='Network Access'):
    """Prepare project, work type, fields and project screen using an admin caller."""
    if (type(apply) is not bool
            or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,9}', project_key)
            or not isinstance(issue_type_name, str)
            or not 1 <= len(issue_type_name) <= 60
            or issue_type_name != issue_type_name.strip()
            or any(ord(char) < 32 or 127 <= ord(char) <= 159
                   for char in issue_type_name)):
        raise JiraProvisionError('Invalid Jira preparation options')
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
    if project.get('simplified') is True:
        raise JiraProvisionError('Use a company-managed Jira project')

    issue_types = call('/rest/api/3/issuetype')
    matches = [item for item in issue_types if item.get('name') == issue_type_name
               and item.get('subtask') is not True]
    if len(matches) > 1:
        raise JiraProvisionError(
            'More than one work type named %r exists; choose a unique --work-type-name'
            % issue_type_name)
    if not matches:
        if not apply:
            return {'ready': False, 'planned': created + ['issue-type:' + issue_type_name],
                    'manual': ['Install the Forge app before apply']}
        issue_type = call('/rest/api/3/issuetype', method='POST', body={
            'name': issue_type_name, 'description': MARKER, 'type': 'standard'})
        created.append('issue-type:' + issue_type_name)
    else:
        issue_type = matches[0]
    issue_type_id = _identifier(issue_type.get('id'), 'issue type ID')
    associated = any(str(item.get('id')) == issue_type_id
                     for item in project.get('issueTypes', []))
    if not associated and apply:
        schemes = call('/rest/api/3/issuetypescheme/project',
                       query={'projectId': project_id})
        values = schemes.get('values', []) if isinstance(schemes, dict) else []
        scheme = values[0].get('issueTypeScheme') if values else None
        scheme_id = _identifier(scheme.get('id') if isinstance(scheme, dict) else '',
                                'issue type scheme ID')
        call('/rest/api/3/issuetypescheme/' + scheme_id + '/issuetype', method='PUT',
             body={'issueTypeIds': [issue_type_id]})
        created.append('issue-type-association:' + project_key)

    fields = call('/rest/api/3/field')
    forge_matches = [field for field in fields
                     if field.get('name') == 'Мережеві доступи AlgoSec']
    forge_matches = [field for field in forge_matches
                     if str(field.get('schema', {}).get('custom', '')).endswith(
                         '/static/algosec-network-access')]
    if len(forge_matches) != 1:
        raise JiraProvisionError(
            'Install the repository Forge app; its structured field was not found uniquely')
    selected = {'structured': forge_matches[0]['id']}
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

    # New projects already have a working default workflow. The bus maps to its standard
    # To Do / In Progress / Done states; creating another workflow would add no value.
    return {
        'ready': bool(apply), 'project_key': project_key, 'project_id': project_id,
        'issue_type_id': issue_type_id, 'issue_type_name': issue_type_name,
        'fields': {'structured': selected['structured'],
                   'id': selected['FireFlow Request ID'],
                   'status': selected['FireFlow Status'],
                   'owner': selected['FireFlow Owner']},
        'screen_id': screen_id, 'screen_scheme_id': screen_scheme_id,
        'issue_type_screen_scheme_id': type_screen_scheme_id, 'created': created,
        'manual': ['Mark the Forge structured field Required if the tenant field-scheme API is unavailable'],
    }


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Prepare Jira Cloud for Jira-FireFlow')
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--project-key', '--space-key', dest='project_key')
    parser.add_argument('--project-name', '--space-name', dest='project_name')
    parser.add_argument('--work-type-name')
    parser.add_argument('--space-only', action='store_true',
                        help='create or verify only the company-managed Jira Space')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    base_url = https_origin(args.base_url)
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
                              issue_type_name=issue_type_name)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get('ready') else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (JiraProvisionError, ConsoleError) as error:
        print('Jira preparation stopped:', str(error))
        raise SystemExit(1)
