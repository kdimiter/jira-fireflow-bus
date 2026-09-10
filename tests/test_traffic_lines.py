"""One issue can ask for several access lines, and a table is how a person writes them.

A FireFlow traffic request carries a list of lines, each with its own source, destination,
service and action. Jira has no repeating group of custom fields, so four custom fields can
only ever express one line. A table in the description expresses four, and the rows are
already held apart in the stored document -- flattening them to text and guessing where one
line ends is how an integration opens the wrong port.
"""
import unittest

from algosec_jira_bus.adf import read_table, rows, tables
from algosec_jira_bus.sync import MappingError, build, endpoint


def cell(text, kind='tableCell'):
    return {'type': kind, 'content': [{'type': 'paragraph',
                                       'content': [{'type': 'text', 'text': text}]}]}


def table(grid, header_kind='tableHeader'):
    body = [{'type': 'tableRow', 'content': [cell(text, header_kind) for text in grid[0]]}]
    body += [{'type': 'tableRow', 'content': [cell(text) for text in line]} for line in grid[1:]]
    return {'type': 'table', 'content': body}


def document(*nodes):
    return {'type': 'doc', 'version': 1, 'content': list(nodes)}


def paragraph(text):
    return {'type': 'paragraph', 'content': [{'type': 'text', 'text': text}]}


ACCESS = [['Source', 'Destination', 'Service'],
          ['192.0.2.0/24', '198.51.100.5', 'tcp/443'],
          ['srv-app-01', 'db-cluster', 'tcp/5432']]

COLUMNS = {'source': 'Source', 'destination': 'Destination', 'service': 'Service',
           'action': 'Action'}
TABLE_MAPPING = {'action': {'field': 'cf_action', 'values': {'Open': 'Allow', 'Close': 'Drop'}},
                 'table': {'field': 'description', 'columns': COLUMNS}}


def issue(description, action='Open', key='NET-12'):
    return {'id': str(1000 + int(key.rsplit('-', 1)[-1])), 'key': key,
            'fields': {'summary': 'Open access', 'cf_action': action,
                                   'description': description}}


class Reading(unittest.TestCase):
    def test_a_table_is_found_anywhere_in_the_document(self):
        found = tables(document(paragraph('context'), table(ACCESS)))
        self.assertEqual(len(found), 1)
        self.assertEqual(rows(found[0])[1], ['192.0.2.0/24', '198.51.100.5', 'tcp/443'])

    def test_header_cells_are_read_whether_or_not_jira_marked_them_as_headers(self):
        for kind in ('tableHeader', 'tableCell'):
            with self.subTest(kind=kind):
                parsed, found = read_table(document(table(ACCESS, kind)), COLUMNS)
                self.assertEqual(len(parsed), 2)
                self.assertEqual(found, ['destination', 'service', 'source'])

    def test_headings_match_without_regard_to_case_and_space(self):
        grid = [['  source ', 'DESTINATION', 'Service'], ['192.0.2.1', '192.0.2.2', 'tcp/22']]
        parsed, _ = read_table(document(table(grid)), COLUMNS)
        self.assertEqual(parsed[0]['source'], '192.0.2.1')

    def test_a_table_about_something_else_is_not_mistaken_for_the_traffic(self):
        window = [['Start', 'End'], ['2026-09-10', '2026-09-11']]
        parsed, _ = read_table(document(table(window), table(ACCESS)), COLUMNS, minimum=2)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]['source'], '192.0.2.0/24')

    def test_a_blank_row_is_spacing_and_not_a_request_for_nothing(self):
        grid = ACCESS + [['', '', ''], ['198.51.100.1', '198.51.100.2', 'tcp/80']]
        parsed, _ = read_table(document(table(grid)), COLUMNS)
        self.assertEqual(len(parsed), 3)

    def test_a_document_with_no_table_yields_nothing_rather_than_guessing(self):
        self.assertEqual(read_table(document(paragraph('192.0.2.1 to 192.0.2.2 on 443')), COLUMNS),
                         ([], []))

    def test_nothing_at_all_is_handled(self):
        for empty in (None, {}, [], 'text'):
            self.assertEqual(read_table(empty, COLUMNS), ([], []))


class Endpoints(unittest.TestCase):
    def test_an_address_is_sent_as_an_address(self):
        for value in ('192.0.2.1', '192.0.2.0/24', '192.0.2.1 - 192.0.2.9', '*',
                      '2001:cdba::3257:9652', 'fe80::1/64'):
            with self.subTest(value=value):
                self.assertEqual(endpoint(value), {'address': value})

    def test_an_object_name_is_sent_as_a_name(self):
        for value in ('srv-app-01', 'db_cluster', 'DMZ-Web', '192.0.2.1x'):
            with self.subTest(value=value):
                self.assertEqual(endpoint(value), {'name': value})


class Building(unittest.TestCase):
    def test_each_row_of_the_table_becomes_its_own_traffic_line(self):
        _, request = build(issue(document(table(ACCESS))), TABLE_MAPPING, 'T', ['fw1'])
        self.assertEqual(len(request['traffic']), 2)
        self.assertEqual(request['traffic'][0]['source']['items'], [{'address': '192.0.2.0/24'}])
        self.assertEqual(request['traffic'][1]['source']['items'], [{'name': 'srv-app-01'}])
        self.assertEqual(request['traffic'][1]['destination']['items'], [{'name': 'db-cluster'}])

    def test_an_action_in_a_custom_field_applies_to_every_row(self):
        _, request = build(issue(document(table(ACCESS)), action='Close'),
                           TABLE_MAPPING, 'T', ['fw1'])
        self.assertEqual([line['action'] for line in request['traffic']], ['Drop', 'Drop'])

    def test_an_action_column_overrides_the_field_row_by_row(self):
        grid = [['Source', 'Destination', 'Service', 'Action'],
                ['192.0.2.1', '192.0.2.2', 'tcp/443', 'allow'],
                ['192.0.2.3', '192.0.2.4', 'tcp/22', 'Drop']]
        _, request = build(issue(document(table(grid)), action='Open'),
                           TABLE_MAPPING, 'T', ['fw1'])
        self.assertEqual([line['action'] for line in request['traffic']], ['Allow', 'Drop'])

    def test_several_values_in_one_cell_are_read_the_way_a_person_types_them(self):
        grid = [['Source', 'Destination', 'Service'],
                ['192.0.2.1, 192.0.2.2', '192.0.2.9', 'tcp/443; tcp/80']]
        _, request = build(issue(document(table(grid))), TABLE_MAPPING, 'T', ['fw1'])
        line = request['traffic'][0]
        self.assertEqual(line['source']['items'], [{'address': '192.0.2.1'}, {'address': '192.0.2.2'}])
        self.assertEqual([item['service'] for item in line['service']['items']],
                         ['tcp/443', 'tcp/80'])

    def test_a_bad_cell_names_the_row_it_is_in(self):
        grid = [['Source', 'Destination', 'Service'],
                ['192.0.2.1', '192.0.2.2', 'tcp/443'],
                ['$(whoami)', '192.0.2.4', 'tcp/22']]
        with self.assertRaises(MappingError) as caught:
            build(issue(document(table(grid))), TABLE_MAPPING, 'T', ['fw1'])
        self.assertIn('row 2', str(caught.exception))

    def test_an_empty_cell_is_refused_rather_than_sent_as_any(self):
        grid = [['Source', 'Destination', 'Service'], ['192.0.2.1', '', 'tcp/443']]
        with self.assertRaises(MappingError) as caught:
            build(issue(document(table(grid))), TABLE_MAPPING, 'T', ['fw1'])
        self.assertIn('destination', str(caught.exception))

    def test_a_description_with_no_table_is_refused_with_the_headings_it_wanted(self):
        with self.assertRaises(MappingError) as caught:
            build(issue(document(paragraph('please open 443'))), TABLE_MAPPING, 'T', ['fw1'])
        self.assertIn('Source', str(caught.exception))

    def test_more_lines_than_fireflow_accepts_are_refused_before_submission(self):
        grid = [['Source', 'Destination', 'Service']]
        grid += [['192.0.2.%d' % (n % 250), '198.51.100.1', 'tcp/443'] for n in range(501)]
        with self.assertRaises(MappingError) as caught:
            build(issue(document(table(grid))), TABLE_MAPPING, 'T', ['fw1'])
        self.assertIn('100', str(caught.exception))

    def test_the_single_field_mapping_still_produces_exactly_one_line(self):
        mapping = {'action': {'field': 'cf_action', 'values': {'Open': 'Allow'}},
                   'source': {'field': 'cf_src'}, 'destination': {'field': 'cf_dst'},
                   'service': {'field': 'cf_svc'}}
        plain_issue = {'key': 'NET-1', 'fields': {'summary': 's', 'cf_action': 'Open',
                                                  'cf_src': '192.0.2.1', 'cf_dst': '192.0.2.2',
                                                  'cf_svc': 'tcp/443'}}
        _, request = build(plain_issue, mapping, 'T', ['fw1'])
        self.assertEqual(len(request['traffic']), 1)

    def test_the_jira_key_still_travels_with_a_multi_line_request(self):
        key, request = build(issue(document(table(ACCESS))), TABLE_MAPPING, 'T', ['fw1'])
        fields = {item['name']: item['values'] for item in request['fields']}
        self.assertTrue(fields['subject'][0].startswith(key + ':'))
        self.assertNotIn('externalId', fields)


if __name__ == '__main__':
    unittest.main()
