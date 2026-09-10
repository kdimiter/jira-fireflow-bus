"""Read a table out of a Jira rich-text field, keeping the rows apart.

Jira stores issue descriptions and textarea custom fields as ADF -- a document tree, not
text. ``jira.plain`` flattens that tree to a string, which is right for a summary and wrong
for anything structured: a table of access lines comes back as one run of words with the
row boundaries gone, and no amount of splitting puts them back.

That matters because a FireFlow traffic request is a *list* of lines, each with its own
source, destination, service and action. Jira has no repeating group of custom fields, so
the natural way for a person to ask for four lines is a four-row table -- and the natural
way to read it back is to walk the tree that already holds those rows apart.

Only tables are parsed here. Free-form prose is deliberately not: guessing which words in a
sentence are an address is how an integration ends up opening the wrong port, and a person
who has to fill in a table has been told exactly what is expected of them.
"""

# ADF wraps header cells in tableHeader when a table has a header row and in tableCell when
# it does not. Both appear in real documents, sometimes in the same table.
CELLS = ('tableCell', 'tableHeader')


def _nodes(node, kind):
    """Direct children of ``node`` with this type."""
    return [child for child in (node or {}).get('content') or []
            if isinstance(child, dict) and child.get('type') == kind]


def tables(document):
    """Every table in the document, outermost first, including nested ones."""
    found = []
    if isinstance(document, dict):
        if document.get('type') == 'table':
            found.append(document)
        for child in document.get('content') or []:
            found.extend(tables(child))
    elif isinstance(document, list):
        for child in document:
            found.extend(tables(child))
    return found


def cell_text(node):
    """Preserve entered line breaks without splitting inline formatting into words."""
    if not isinstance(node, dict):
        return ''
    if node.get('type') == 'text':
        return node.get('text', '')
    if node.get('type') == 'hardBreak':
        return '\n'
    text = ''.join(cell_text(child) for child in node.get('content') or [])
    return text + ('\n' if node.get('type') in ('paragraph', 'listItem') else '')


def rows(table):
    """The table as a list of lists of cell text, header row included."""
    result = []
    for row in _nodes(table, 'tableRow'):
        cells = [child for child in row.get('content') or []
                 if isinstance(child, dict) and child.get('type') in CELLS]
        result.append([cell_text(cell).strip() for cell in cells])
    return result


def read_table(document, columns, minimum=1):
    """Rows of ``{name: text}`` keyed by the wanted columns.

    ``columns`` maps a name this code uses to the heading a person types, for example
    ``{'source': 'Source'}``. Headings are matched without regard to case or surrounding
    space, because a heading is typed by hand and a capital letter is not consent to fail.

    The first table with at least ``minimum`` of the wanted headings wins. That rule lets a
    description hold a table of something else entirely -- a change window, a list of
    approvers -- without it being mistaken for the traffic.

    Returns ``(rows, headings)``; ``headings`` is which wanted columns were actually found,
    so the caller can tell "no such column" from "column present but empty".
    """
    wanted = {name: str(heading).strip().casefold() for name, heading in (columns or {}).items()}
    for table in tables(document):
        grid = rows(table)
        if len(grid) < 2:
            continue
        header = [cell.strip().casefold() for cell in grid[0]]
        found = {name: header.index(heading) for name, heading in wanted.items()
                 if heading in header}
        if len(found) < max(1, minimum):
            continue
        parsed = []
        for line in grid[1:]:
            if not any(cell for cell in line):
                continue  # a blank row is spacing, not a request for nothing
            parsed.append({name: (line[index] if index < len(line) else '')
                           for name, index in found.items()})
        return parsed, sorted(found)
    return [], []
