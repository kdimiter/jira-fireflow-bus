"""Versioned form data, independent of Jira field IDs and FireFlow payloads."""
import ipaddress
import re


def address(item):
    if not isinstance(item, dict):
        raise ValueError('Address must have kind and value')
    kind, value = item.get('kind'), item.get('value')
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Address value is required')
    value = value.strip()
    if kind == 'ip':
        ipaddress.ip_address(value)
    elif kind == 'subnet':
        ipaddress.ip_network(value, strict=True)
    elif kind == 'range':
        parts = re.split(r'\s*[-–]\s*', value)
        if len(parts) != 2:
            raise ValueError('Range needs two addresses')
        first, last = map(ipaddress.ip_address, parts)
        if first.version != last.version or int(first) > int(last):
            raise ValueError('Range endpoints are incompatible or reversed')
        value = '%s-%s' % (first, last)
    elif kind == 'hostname':
        labels = value.rstrip('.').split('.')
        if len(value) > 253 or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', s) for s in labels):
            raise ValueError('Invalid hostname')
        # Keep DNS identity; do not resolve or reinterpret it as a device object.
    else:
        raise ValueError('Unsupported address kind')
    return {'kind': kind, 'value': value}


def normalize(raw):
    if not isinstance(raw, dict) or type(raw.get('schemaVersion')) is not int or raw['schemaVersion'] != 1:
        raise ValueError('Expected structured request schemaVersion 1')
    justification = raw.get('justification')
    if not isinstance(justification, str) or not justification.strip():
        raise ValueError('Business justification is required')
    action = raw.get('changeType')
    if action not in ('Allow', 'Drop'):
        raise ValueError('changeType must be Allow or Drop')
    if raw.get('duration', {'kind': 'permanent'}) != {'kind': 'permanent'}:
        raise ValueError('Temporary access requires a verified expiration adapter')
    rows = raw.get('trafficLines')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('Expected 1..100 traffic lines')
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Traffic line must be an object')
        services = row.get('services')
        if not isinstance(services, list) or not 1 <= len(services) <= 100:
            raise ValueError('Expected 1..100 services')
        normalized = []
        for service in services:
            if not isinstance(service, dict) or service.get('kind') != 'port':
                raise ValueError('Unsupported service kind')
            protocol, port = service.get('protocol'), service.get('port')
            if protocol not in ('tcp', 'udp') or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError('Expected TCP/UDP port 1..65535')
            normalized.append('%s/%d' % (protocol, port))
        result.append({'source': address(row.get('source')),
                       'destination': address(row.get('destination')),
                       'services': normalized})
    return {'justification': justification.strip(), 'action': action, 'lines': result}
