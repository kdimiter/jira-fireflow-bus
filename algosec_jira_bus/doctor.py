"""One command that asks both systems whether this configuration would actually work.

Everything here is read-only. Nothing is created, commented, transitioned or closed, so it
is safe to run against production at any hour, including before the bus has ever been
allowed to write anything.

The point is not to produce a tidy report. It is that a bus configured with one wrong
custom field id, or a template name that is a letter off, fails in ways that look like
nothing happening: issues are refused one by one into a log nobody is watching, or the
poll returns and writes nothing. Each check below therefore ends in a sentence saying what
to change, not merely that something is wrong.

Checks are grouped by what they need. A section whose system is unreachable is reported as
unreachable and the rest still run: finding out that the FireFlow template is missing is
useful even on a day when Jira is down.
"""
from .adf import read_table
from .jira import Jira
from .jira_updates import validate_jira_to_fireflow
from .queue import Failures
from .sync import build, MappingError, validate_for_adapter, State, mapped_fields, intake_mode

OK, WARN, FAIL = 'ok', 'warn', 'fail'
# The fields sync.build() always emits. A deployment that does not allowlist these cannot
# submit anything, and the refusal happens inside the adapter where it is easy to misread.
REQUIRED_FIELDS = ('subject', 'devices', 'Requestor')


def note(level, check, detail=''):
    return {'level': level, 'check': check, 'detail': detail}


def local(settings, state):
    """What can be judged without asking either system anything."""
    results = []
    try:
        intake_mode(settings)
    except MappingError as error:
        results.append(note(FAIL, 'intake', str(error)))
    mapping = settings.get('mapping') or {}
    structured = 'structured' in mapping
    if structured:
        spec = mapping['structured']
        conflicts = sorted(set(mapping) & {'table', 'action', 'source', 'destination', 'service'})
        if not isinstance(spec, dict) or not isinstance(spec.get('field'), str) or not spec['field'].strip():
            results.append(note(FAIL, 'mapping.structured',
                                'A structured mapping needs a nonempty Jira field id in "field".'))
        elif conflicts:
            results.append(note(FAIL, 'mapping.structured',
                                'Remove legacy mappings when using structured input: %s.'
                                % ', '.join(conflicts)))
        else:
            results.append(note(OK, 'mapping.structured', 'reads structured data from %s' % spec['field']))
    else:
        table = mapping.get('table')
        if table is not None and not isinstance(table, dict):
            results.append(note(FAIL, 'mapping.table', 'A table mapping must be an object.'))
            table = None
        if table:
            columns = table.get('columns') or {}
            if not columns:
                results.append(note(FAIL, 'mapping.table.columns',
                                    'Name the headings: {"source": "Source", ...} as they are '
                                    'spelled in the ticket.'))
            else:
                missing = [n for n in ('source', 'destination', 'service') if n not in columns]
                results.append(note(FAIL if missing else OK, 'mapping.table.columns',
                                    ('No heading configured for: %s' % ', '.join(missing)) if missing
                                    else 'reads %s from %r' % (', '.join(sorted(columns)),
                                                               table.get('field') or 'description')))
            if 'action' not in (table.get('columns') or {}) and not mapping.get('action'):
                results.append(note(FAIL, 'mapping.action',
                                    'With no Action column, the action must come from a field.'))
        # Without a table, one line is built from four single-value entries and all are required.
        for wanted in (('action',) if table else ('action', 'source', 'destination', 'service')):
            spec = mapping.get(wanted)
            if table and wanted == 'action' and 'action' in (table.get('columns') or {}):
                continue
            if not isinstance(spec, dict) or not (spec.get('field') or spec.get('constant')):
                results.append(note(FAIL, 'mapping.%s' % wanted,
                                    'Add a "field" (a Jira field id) or a "constant" for %s.' % wanted))
        action = mapping.get('action') or {}
        if isinstance(action, dict) and action.get('field') and not action.get('values'):
            results.append(note(FAIL, 'mapping.action.values',
                                'An action field needs a "values" map onto Allow / Drop, or every '
                                'issue is refused as unmapped.'))

    fireflow = settings.get('fireflow') or {}
    try:
        reverse = validate_jira_to_fireflow(settings.get('jira_to_fireflow'))
    except ValueError as error:
        reverse = None
        results.append(note(FAIL, 'jira_to_fireflow.config', str(error)))
    if reverse is not None:
        if fireflow.get('legacy_rt_enabled') is not True:
            results.append(note(
                FAIL, 'jira_to_fireflow.transport',
                'Enable fireflow.legacy_rt_enabled to deliver mapped Jira updates.'))
        else:
            results.append(note(
                OK, 'jira_to_fireflow.transport',
                'internal comments and %d mapped statuses use FireFlow RT REST'
                % len(reverse['status_map'])))
    devices = list(fireflow.get('devices') or [])
    allowed = list(fireflow.get('allowed_devices') or [])
    if not devices:
        results.append(note(FAIL, 'fireflow.devices',
                            'Name the devices explicitly. FireFlow refuses a traffic request '
                            'without them, and planning must not silently target everything.'))
    elif '*' not in allowed and set(devices) - set(allowed):
        results.append(note(FAIL, 'fireflow.allowed_devices',
                            'These devices are used but not allowlisted, so the adapter will '
                            'refuse every request: %s' % ', '.join(sorted(set(devices) - set(allowed)))))
    else:
        results.append(note(OK, 'fireflow.devices', ', '.join(devices)))

    template = fireflow.get('template')
    if not template:
        results.append(note(FAIL, 'fireflow.template', 'Name the traffic change template to use.'))
    elif '*' not in (fireflow.get('allowed_templates') or []) and \
            template not in (fireflow.get('allowed_templates') or []):
        results.append(note(FAIL, 'fireflow.allowed_templates',
                            'Template %r is not allowlisted, so the adapter refuses before '
                            'anything is sent.' % template))

    fields = fireflow.get('allowed_fields') or []
    required = REQUIRED_FIELDS + (('Change Request Description',) if structured else ())
    missing = [name for name in required if '*' not in fields and name not in fields]
    if missing:
        results.append(note(FAIL, 'fireflow.allowed_fields',
                            'The bus always sends these and they are not allowlisted: %s'
                            % ', '.join(missing)))

    parked = Failures(state).parked()
    if parked:
        results.append(note(WARN, 'queue',
                            '%d issue(s) parked and waiting for a person: %s'
                            % (len(parked), ', '.join(sorted(parked)))))
    return results


def jira(settings, state, client=None):
    """Ask the tenant whether this mapping and this query describe anything real."""
    results = []
    try:
        client = client or Jira(settings['jira'])
    except Exception as error:
        return [note(FAIL, 'jira.config', str(error))]

    try:
        me = client.myself()
        results.append(note(OK, 'jira.auth', 'authenticated as %s'
                            % (me.get('displayName') or me.get('emailAddress') or 'unknown')))
    except Exception as error:
        return results + [note(FAIL, 'jira.auth',
                               'Could not authenticate (%s). Check base_url, email and the '
                               'token behind token_ref.' % type(error).__name__)]

    wanted = sorted(mapped_fields(settings.get('mapping')))
    try:
        known = {entry.get('id'): entry.get('name') for entry in client.fields()
                 if isinstance(entry, dict)}
    except Exception as error:
        results.append(note(WARN, 'jira.fields',
                            'Could not list fields (%s), so the mapping is unverified. A very '
                            'large tenant can exceed the client response limit.'
                            % type(error).__name__))
        known = None

    if known is not None:
        for field in wanted:
            if field in known:
                results.append(note(OK, 'mapping -> %s' % field, known[field] or ''))
            else:
                results.append(note(FAIL, 'mapping -> %s' % field,
                                    'No such field in this tenant. Run the "fields" command and '
                                    'take the id whose name matches the field on your screen.'))

    limit = settings['jira'].get('limit', 50)
    try:
        issues = client.search(settings['jira']['jql'], set(wanted) | {'summary'}, limit=limit)
    except Exception as error:
        return results + [note(FAIL, 'jira.jql',
                               'The query was rejected (%s). Check the JQL and that this account '
                               'can see the project.' % type(error).__name__)]

    results.append(note(OK if issues else WARN, 'jira.jql',
                        '%d issue(s) across all pages%s' % (len(issues),
                        '' if issues else '; nothing to do, which may be correct')))

    if issues:
        blank = [field for field in wanted
                 if not any((issue.get('fields') or {}).get(field) for issue in issues)]
        if blank:
            results.append(note(WARN, 'jira.values',
                                'Mapped but empty on every issue in this query: %s. Either the id '
                                'is wrong or the field is not on this issue type.'
                                % ', '.join(blank)))

    table = (settings.get('mapping') or {}).get('table')
    if issues and isinstance(table, dict) and (table.get('columns') or {}):
        # The single most useful thing to know before turning writing on: does the table a
        # person actually typed match the headings this configuration expects? A mismatch
        # refuses every issue one at a time, into a log, and looks like an empty queue.
        field = table.get('field') or 'description'
        counts = [len(read_table((issue.get('fields') or {}).get(field),
                                 table['columns'],
                                 minimum=table.get('minimum_columns', 2))[0])
                  for issue in issues]
        readable = sum(1 for count in counts if count)
        results.append(note(OK if readable == len(issues) else
                            (WARN if readable else FAIL), 'mapping.table',
                            '%d of %d issue(s) carry a readable table, %d traffic line(s) in '
                            'total. Headings must match exactly (case and spacing aside): %s'
                            % (readable, len(issues), sum(counts),
                               ', '.join(sorted(table['columns'].values())))))

    # A readable table is not necessarily a valid FireFlow request for this bus release.
    fireflow_config = settings.get('fireflow') or {}
    for issue in issues:
        try:
            _, request = build(issue, settings.get('mapping') or {},
                               fireflow_config.get('template', ''),
                               fireflow_config.get('devices') or [])
            validate_for_adapter(request)
        except (MappingError, KeyError) as error:
            results.append(note(FAIL, 'mapping.request',
                                '%s: %s' % (issue.get('key', '?'), error)))

    targets = list(((settings.get('mirror') or {}).get('transitions') or {}).values())
    if targets and issues:
        try:
            offered = {name.casefold() for name in client.transitions(issues[0].get('key'))}
            unknown = [name for name in targets if name.casefold() not in offered]
            results.append(note(WARN if unknown else OK, 'jira.transitions',
                                ('Not offered on %s right now: %s. A transition that never '
                                 'matches fires nothing and fails quietly.'
                                 % (issues[0].get('key'), ', '.join(unknown))) if unknown
                                else 'configured transitions exist on %s' % issues[0].get('key')))
        except Exception as error:
            results.append(note(WARN, 'jira.transitions',
                                'Could not read transitions (%s).' % type(error).__name__))
    return results


def fireflow(settings, adapter):
    """Ask the appliance whether the named template is one it will actually accept."""
    wanted = (settings.get('fireflow') or {}).get('template')
    try:
        templates = (adapter.templates() or {}).get('data') or []
    except Exception as error:
        return [note(FAIL, 'fireflow.auth',
                     'Could not read templates (%s). Check base_url, the TLS pin, the account '
                     'behind password_ref, and that it exists in the FireFlow registry -- an AFA '
                     'login is not enough.' % type(error).__name__)]

    usable = [entry for entry in templates if isinstance(entry, dict)
              and entry.get('enabled') is True and entry.get('type') == 'Traffic Change']
    if any(entry.get('name') == wanted for entry in usable):
        return [note(OK, 'fireflow.template', '%r is enabled and of type Traffic Change' % wanted)]
    names = sorted(str(entry.get('name')) for entry in usable)
    return [note(FAIL, 'fireflow.template',
                 'Template %r is not available to this account. Enabled traffic templates here: %s'
                 % (wanted, ', '.join(names) if names else 'none'))]


def run(settings, adapter, state, client=None, log=print):
    """Every check, grouped, with a verdict. Returns the findings and whether any failed."""
    results = []
    for section, findings in (('local', local(settings, state)),
                              ('jira', jira(settings, state, client)),
                              ('fireflow', fireflow(settings, adapter))):
        for finding in findings:
            finding['section'] = section
            results.append(finding)
    for finding in results:
        log('%-5s %-28s %s' % (finding['level'].upper(), finding['check'], finding['detail']))
    failed = [finding for finding in results if finding['level'] == FAIL]
    log('')
    log('%d checks, %d failed, %d warnings'
        % (len(results), len(failed), sum(1 for f in results if f['level'] == WARN)))
    if failed:
        log('The bus would not work as configured. Fix the failures above and run this again.')
    return {'results': results, 'failed': failed}
