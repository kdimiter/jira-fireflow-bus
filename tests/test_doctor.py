"""The preflight has one job: turn silent misconfiguration into a sentence you can act on.

Each test here corresponds to a way this bus fails quietly in the field -- a custom field
id that is one digit off, a template name that does not exist, a transition the board does
not offer, a first page already full of issues the bus has handled. None of those raise;
they all just produce a poll that does nothing.
"""
from pathlib import Path
import tempfile
import unittest

from algosec_jira_bus import doctor
from algosec_jira_bus.queue import Failures
from algosec_jira_bus.sync import State


def settings(**over):
    base = {'jira': {'base_url': 'https://t.atlassian.net', 'email': 'a@b.c',
                     'token_ref': 'env:X', 'jql': 'project = NET', 'limit': 2},
            'fireflow': {'template': 'Traffic Change Request', 'devices': ['fw1'],
                         'allowed_templates': ['Traffic Change Request'],
                         'allowed_devices': ['fw1'],
                         'allowed_fields': ['subject', 'devices', 'Requestor']},
            'mapping': {'action': {'field': 'customfield_1', 'values': {'Open': 'Allow'}},
                        'source': {'field': 'customfield_2'},
                        'destination': {'field': 'customfield_3'},
                        'service': {'field': 'customfield_4'}},
            'mirror': {'transitions': {'resolved': 'Done'}}}
    for section, changes in over.items():
        if changes is None:
            base.pop(section, None)
        elif isinstance(base.get(section), dict) and isinstance(changes, dict):
            base[section] = {**base[section], **changes}
        else:
            base[section] = changes
    return base


def issue(key='NET-1', **fields):
    body = {'summary': 'x', 'customfield_1': 'Open', 'customfield_2': '192.0.2.1',
            'customfield_3': '192.0.2.2', 'customfield_4': 'tcp/443',
            'creator': {'displayName': 'Ticket Creator',
                        'emailAddress': 'creator@example.org'}}
    body.update(fields)
    return {'key': key, 'fields': body}


class Jira:
    def __init__(self, fields=None, issues=None, transitions=None, fail=None):
        self._fields = fields if fields is not None else [
            {'id': 'customfield_%d' % n, 'name': 'Field %d' % n, 'custom': True,
             'schema': {'type': 'string'}} for n in (1, 2, 3, 4)]
        self._issues = issues if issues is not None else [issue()]
        self._transitions = transitions if transitions is not None else ['Done', 'In Progress']
        self.fail = fail or set()

    def myself(self):
        if 'auth' in self.fail: raise ValueError('denied')
        return {'displayName': 'Bus Automation'}

    def fields(self):
        if 'fields' in self.fail: raise ValueError('too big')
        return self._fields

    def search(self, jql, wanted, limit=50):
        if 'search' in self.fail: raise ValueError('bad jql')
        return self._issues

    def transitions(self, key):
        if 'transitions' in self.fail: raise ValueError('nope')
        return self._transitions


class Fireflow:
    def __init__(self, templates=None, fail=False):
        self._templates = templates if templates is not None else [
            {'name': 'Traffic Change Request', 'enabled': True, 'type': 'Traffic Change'}]
        self.fail = fail

    def templates(self):
        if self.fail: raise ValueError('unreachable')
        return {'data': self._templates}


class Base(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = State(Path(self.directory.name) / 'state' / 'sync.json')

    def tearDown(self):
        self.directory.cleanup()

    def levels(self, results):
        return {finding['check']: finding['level'] for finding in results}

    def detail(self, results, check):
        return next(f['detail'] for f in results if f['check'] == check)


class Local(Base):
    def test_a_sound_configuration_raises_nothing(self):
        results = doctor.local(settings(), self.state)
        self.assertNotIn(doctor.FAIL, [f['level'] for f in results])

    def test_a_device_that_is_used_but_not_allowlisted_is_a_failure(self):
        results = doctor.local(settings(fireflow={'allowed_devices': ['other']}), self.state)
        self.assertEqual(self.levels(results)['fireflow.allowed_devices'], doctor.FAIL)
        self.assertIn('fw1', self.detail(results, 'fireflow.allowed_devices'))

    def test_a_template_that_is_not_allowlisted_is_a_failure(self):
        results = doctor.local(settings(fireflow={'allowed_templates': ['Something else']}), self.state)
        self.assertEqual(self.levels(results)['fireflow.allowed_templates'], doctor.FAIL)

    def test_the_fields_the_bus_always_sends_must_be_allowlisted(self):
        results = doctor.local(settings(fireflow={'allowed_fields': ['subject']}), self.state)
        detail = self.detail(results, 'fireflow.allowed_fields')
        self.assertIn('devices', detail)
        self.assertIn('Requestor', detail)
        self.assertNotIn('externalId', detail)

    def test_an_action_field_without_a_values_map_refuses_every_issue(self):
        broken = settings()
        broken['mapping']['action'] = {'field': 'customfield_1'}
        results = doctor.local(broken, self.state)
        self.assertEqual(self.levels(results)['mapping.action.values'], doctor.FAIL)

    def test_a_missing_mapping_entry_is_named(self):
        broken = settings()
        del broken['mapping']['service']
        results = doctor.local(broken, self.state)
        self.assertEqual(self.levels(results)['mapping.service'], doctor.FAIL)

    def test_parked_failures_are_surfaced_as_a_warning(self):
        Failures(self.state).record('NET-9', 'create', ValueError('unknown'), retryable=False)
        results = doctor.local(settings(), self.state)
        self.assertEqual(self.levels(results)['queue'], doctor.WARN)
        self.assertIn('NET-9', self.detail(results, 'queue'))


class JiraSide(Base):
    def test_a_field_id_that_does_not_exist_in_the_tenant_is_a_failure(self):
        client = Jira(fields=[{'id': 'customfield_1', 'name': 'Access action', 'custom': True}])
        results = doctor.jira(settings(), self.state, client)
        self.assertEqual(self.levels(results)['mapping -> customfield_2'], doctor.FAIL)
        self.assertIn('fields', self.detail(results, 'mapping -> customfield_2'))

    def test_a_field_that_exists_is_reported_with_its_human_name(self):
        results = doctor.jira(settings(), self.state, Jira())
        self.assertEqual(self.detail(results, 'mapping -> customfield_1'), 'Field 1')

    def test_bad_credentials_stop_the_section_and_say_what_to_check(self):
        results = doctor.jira(settings(), self.state, Jira(fail={'auth'}))
        self.assertEqual(results[-1]['level'], doctor.FAIL)
        self.assertIn('token_ref', results[-1]['detail'])

    def test_a_rejected_query_is_a_failure_not_an_empty_result(self):
        results = doctor.jira(settings(), self.state, Jira(fail={'search'}))
        self.assertEqual(self.levels(results)['jira.jql'], doctor.FAIL)

    def test_a_query_that_matches_nothing_warns_but_does_not_fail(self):
        results = doctor.jira(settings(), self.state, Jira(issues=[]))
        self.assertEqual(self.levels(results)['jira.jql'], doctor.WARN)

    def test_results_across_pages_do_not_warn_about_first_page_starvation(self):
        client = Jira(issues=[issue('NET-1'), issue('NET-2'), issue('NET-3')])
        results = doctor.jira(settings(), self.state, client)   # limit is 2
        self.assertNotIn('jira.page', self.levels(results))
        self.assertIn('3 issue(s) across all pages', self.detail(results, 'jira.jql'))

    def test_a_mapped_field_that_is_empty_on_every_issue_is_flagged(self):
        client = Jira(issues=[issue(customfield_3='')])
        results = doctor.jira(settings(), self.state, client)
        self.assertIn('customfield_3', self.detail(results, 'jira.values'))

    def test_a_transition_the_board_does_not_offer_is_flagged(self):
        client = Jira(transitions=['Start progress'])
        results = doctor.jira(settings(), self.state, client)
        self.assertEqual(self.levels(results)['jira.transitions'], doctor.WARN)
        self.assertIn('Done', self.detail(results, 'jira.transitions'))

    def test_a_transition_the_board_offers_in_another_case_is_accepted(self):
        client = Jira(transitions=['done'])
        results = doctor.jira(settings(), self.state, client)
        self.assertEqual(self.levels(results)['jira.transitions'], doctor.OK)

    def test_a_tenant_too_large_to_list_warns_rather_than_failing(self):
        results = doctor.jira(settings(), self.state, Jira(fail={'fields'}))
        self.assertEqual(self.levels(results)['jira.fields'], doctor.WARN)


class FireFlowSide(Base):
    def test_the_configured_template_is_confirmed_enabled_and_of_the_right_type(self):
        results = doctor.fireflow(settings(), Fireflow())
        self.assertEqual(results[0]['level'], doctor.OK)

    def test_a_disabled_template_is_a_failure_and_the_usable_ones_are_listed(self):
        adapter = Fireflow(templates=[
            {'name': 'Traffic Change Request', 'enabled': False, 'type': 'Traffic Change'},
            {'name': '170: Traffic Change (IPv6)', 'enabled': True, 'type': 'Traffic Change'}])
        results = doctor.fireflow(settings(), adapter)
        self.assertEqual(results[0]['level'], doctor.FAIL)
        self.assertIn('170: Traffic Change (IPv6)', results[0]['detail'])

    def test_a_template_of_the_wrong_form_type_does_not_count(self):
        adapter = Fireflow(templates=[
            {'name': 'Traffic Change Request', 'enabled': True, 'type': 'Rule Removal'}])
        self.assertEqual(doctor.fireflow(settings(), adapter)[0]['level'], doctor.FAIL)

    def test_an_unreachable_appliance_says_the_account_must_exist_in_fireflow(self):
        results = doctor.fireflow(settings(), Fireflow(fail=True))
        self.assertEqual(results[0]['level'], doctor.FAIL)
        self.assertIn('AFA login is not enough', results[0]['detail'])


class Whole(Base):
    def test_a_healthy_setup_reports_no_failures(self):
        report = doctor.run(settings(), Fireflow(), self.state, Jira(), log=lambda *a: None)
        self.assertEqual(report['failed'], [])

    def test_each_finding_says_which_system_it_came_from(self):
        report = doctor.run(settings(), Fireflow(), self.state, Jira(), log=lambda *a: None)
        self.assertEqual({f['section'] for f in report['results']},
                         {'local', 'jira', 'fireflow'})

    def test_one_broken_system_does_not_hide_the_others(self):
        report = doctor.run(settings(), Fireflow(fail=True), self.state, Jira(fail={'auth'}),
                            log=lambda *a: None)
        sections = {f['section'] for f in report['failed']}
        self.assertIn('jira', sections)
        self.assertIn('fireflow', sections)

    def test_nothing_is_written_while_checking(self):
        before = self.state.path.read_text() if self.state.path.exists() else None
        doctor.run(settings(), Fireflow(), self.state, Jira(), log=lambda *a: None)
        after = self.state.path.read_text() if self.state.path.exists() else None
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()


class TableMapping(Base):
    """A table mapping has different requirements, and getting them wrong is silent."""

    def table_settings(self, **table):
        base = settings()
        spec = {'field': 'description',
                'columns': {'source': 'Source', 'destination': 'Destination',
                            'service': 'Service'}}
        spec.update(table)
        base['mapping'] = {'action': base['mapping']['action'], 'table': spec}
        return base

    def test_a_table_mapping_does_not_need_the_single_value_traffic_fields(self):
        results = doctor.local(self.table_settings(), self.state)
        self.assertNotIn(doctor.FAIL, [f['level'] for f in results])

    def test_a_table_without_columns_is_a_failure(self):
        results = doctor.local(self.table_settings(columns={}), self.state)
        self.assertEqual(self.levels(results)['mapping.table.columns'], doctor.FAIL)

    def test_a_missing_heading_is_named(self):
        results = doctor.local(self.table_settings(
            columns={'source': 'Source', 'service': 'Service'}), self.state)
        self.assertIn('destination', self.detail(results, 'mapping.table.columns'))

    def test_no_action_anywhere_is_a_failure(self):
        broken = self.table_settings()
        del broken['mapping']['action']
        results = doctor.local(broken, self.state)
        self.assertEqual(self.levels(results)['mapping.action'], doctor.FAIL)

    def test_an_action_column_removes_the_need_for_an_action_field(self):
        settings_with_column = self.table_settings(
            columns={'source': 'Source', 'destination': 'Destination',
                     'service': 'Service', 'action': 'Action'})
        del settings_with_column['mapping']['action']
        results = doctor.local(settings_with_column, self.state)
        self.assertNotIn('mapping.action', self.levels(results))

    def test_the_description_is_requested_even_when_the_table_names_no_field(self):
        spec = self.table_settings()
        del spec['mapping']['table']['field']
        from algosec_jira_bus.sync import mapped_fields
        self.assertIn('description', mapped_fields(spec['mapping']))

    def test_issues_whose_table_cannot_be_read_are_counted_before_writing_is_enabled(self):
        def cell(text):
            return {'type': 'tableCell', 'content': [{'type': 'paragraph',
                    'content': [{'type': 'text', 'text': text}]}]}

        def doc(grid):
            return {'type': 'doc', 'content': [{'type': 'table', 'content': [
                {'type': 'tableRow', 'content': [cell(t) for t in row]} for row in grid]}]}

        good = doc([['Source', 'Destination', 'Service'], ['192.0.2.1', '192.0.2.2', 'tcp/443']])
        wrong = doc([['Src', 'Dst', 'Port'], ['192.0.2.1', '192.0.2.2', 'tcp/443']])
        client = Jira(issues=[issue('NET-1', description=good),
                              issue('NET-2', description=wrong)])
        results = doctor.jira(self.table_settings(), self.state, client)
        detail = self.detail(results, 'mapping.table')
        self.assertEqual(self.levels(results)['mapping.table'], doctor.WARN)
        self.assertIn('1 of 2', detail)

    def test_headings_that_match_nothing_at_all_are_a_failure_not_a_warning(self):
        client = Jira(issues=[issue('NET-1', description={'type': 'doc', 'content': []})])
        results = doctor.jira(self.table_settings(), self.state, client)
        self.assertEqual(self.levels(results)['mapping.table'], doctor.FAIL)


class StructuredLocal(Base):
    def config(self):
        config = settings()
        config['mapping'] = {'structured': {'field': 'customfield_10'}}
        config['fireflow']['allowed_fields'].append('Change Request Description')
        return config

    def test_structured_configuration_needs_no_legacy_mapping(self):
        self.assertNotIn(doctor.FAIL, self.levels(doctor.local(self.config(), self.state)).values())

    def test_structured_field_must_be_nonempty_string(self):
        for spec in (None, {}, 'customfield_10', {'field': ''}, {'field': ' '}, {'field': 10}):
            config = self.config()
            config['mapping']['structured'] = spec
            with self.subTest(spec=spec):
                self.assertEqual(self.levels(doctor.local(config, self.state))['mapping.structured'], doctor.FAIL)

    def test_structured_mapping_rejects_legacy_keys_even_when_empty(self):
        for key in ('table', 'action', 'source', 'destination', 'service'):
            config = self.config()
            config['mapping'][key] = {}
            with self.subTest(key=key):
                self.assertEqual(self.levels(doctor.local(config, self.state))['mapping.structured'], doctor.FAIL)

    def test_structured_requires_description_allowlist(self):
        config = self.config()
        config['fireflow']['allowed_fields'].remove('Change Request Description')
        results = doctor.local(config, self.state)
        self.assertEqual(self.levels(results)['fireflow.allowed_fields'], doctor.FAIL)
        self.assertIn('Change Request Description', self.detail(results, 'fireflow.allowed_fields'))

    def test_wildcard_allows_structured_description(self):
        config = self.config()
        config['fireflow']['allowed_fields'] = ['*']
        self.assertNotIn(doctor.FAIL, self.levels(doctor.local(config, self.state)).values())
