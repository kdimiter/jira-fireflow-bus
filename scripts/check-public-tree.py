#!/usr/bin/env python3
"""Fail closed when a candidate public tree contains known private material or binaries."""
from pathlib import Path
import hashlib
import re
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve()
mac_home = re.escape('/') + 'Users' + re.escape('/')
linux_home = re.escape('/') + 'home' + re.escape('/')
text_rules = (
    ('personal email address', re.compile(r'(?i)\b[\w.+-]+@(gmail|outlook|hotmail|icloud)\.[a-z]{2,}\b')),
    ('local macOS home path', re.compile(mac_home + r'(?!example(?:/|\b))[^/\s]+')),
    ('local Linux home path', re.compile(linux_home + r'(?!example(?:/|\b))[^/\s]+')),
    ('non-placeholder Jira tenant', re.compile(
        r'(?i)(?<![a-z0-9-])(?!your-tenant\.|example\.|tenant\.|t\.|different\.|other\.)[a-z0-9-]+\.atlassian\.net')),
    ('private IPv4 address', re.compile(
        r'(?<![0-9])(?:10\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}|192\.168\.(?:[0-9]{1,3}\.)[0-9]{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.(?:[0-9]{1,3}\.)[0-9]{1,3})(?![0-9])')),
    ('maintainer repository other than this public project', re.compile(
        r'(?i)github\.com/kdimiter/(?!jira-fireflow-bus(?:\.git)?(?:[\s/#?]|$))[a-z0-9_.-]+')),
    ('probable private key filename', re.compile(r'(?i)\b(?!example(?:[_-]))[a-z0-9_.-]+_(ed25519|rsa)\b')),
)
retired_runtime_markers = (
    'algosec' + '_mcp',
    'algosec' + '_host_mcp',
    'algosec' + '-host-mcp',
)
forbidden_suffixes = ('.whl', '.run', '.tar', '.tar.gz', '.tgz', '.zip', '.pem', '.key')
sensitive_names = {'bus.json', 'secrets.json', 'secrets.env'}
sensitive_suffixes = ('.jsonl', '.started.json', '.p12', '.pfx', '.jks', '.kdbx')
sensitive_directories = {'state', 'receipts', 'approvals'}
allowed_screenshots = {
    'docs/screenshots/01-project-details-redacted.png': '060001b879e47c09bd6789dc7a546a859394bcd71c1c33b94375b9d190c2a7e4',
    'docs/screenshots/02-project-access-redacted.png': ''.join((
        '374341a9e2f85199', '9d128fb1acf8e597',
        '1c22392ce0f0df08', '91bcdab3a7b3e259')),
    'docs/screenshots/14-create-space-template.png': '9c0d08ffe5407e7f6d621f84591432c9f34f64aec022c79aed02a03056fd2a79',
    'docs/screenshots/15-space-management-type.png': '7bf8e1b4bfc15ea1db9b1bea239357e865b188f6ae63985519f90236d7125fdb',
    'docs/screenshots/16-space-name-key-access.png': ''.join((
        'cd9deab548be4a6e', 'd6763c0c3cd5cbb4',
        'f5f8a9c9c7739345', '56106c1bce769247')),
    'docs/screenshots/17-space-work-types.png': 'd85d89ad134bcf53ae56ca04b8595d8f886fb6171d6f37d1320b42a323aa76ac',
    'docs/screenshots/18-space-initial-statuses.png': 'f54a01ad782a53c06da9f029daae76366203bf725332771e67632d829e1a15b5',
    'docs/screenshots/21-docker-installer.png': 'f528da657487d6ef09d8c544304ea017d06548bafa4831eb6236bdf25871f333',
    'docs/screenshots/22-bus-conf.png': '5631eb20f621c6f70b052d062a166f17c5736d064a2ffba5675c3d3e9ce84d2f',
    'docs/screenshots/23-structured-network-request.png': 'cab01aa928e1cbde7f9f810600abbd194d2918114515b7150187cb740e333583',
}
violations = []
for path in root.rglob('*'):
    if any(part in {'.git', 'node_modules', '.venv', '__pycache__'}
           or part.endswith('.egg-info') for part in path.parts):
        continue
    relative = path.relative_to(root)
    name = str(relative)
    if path.is_symlink():
        violations.append(name + ': symbolic links are not allowed')
        continue
    if not path.is_file():
        continue
    if name in allowed_screenshots:
        if hashlib.sha256(path.read_bytes()).hexdigest() != allowed_screenshots[name]:
            violations.append(name + ': reviewed screenshot digest changed')
        continue
    if (name.endswith(forbidden_suffixes) or path.name in sensitive_names
            or name.endswith(sensitive_suffixes)
            or any(part in sensitive_directories for part in relative.parts)):
        violations.append(name + ': forbidden public artifact')
        continue
    if path.stat().st_size > 5 * 1024 * 1024:
        violations.append(name + ': file exceeds 5 MiB publication limit')
        continue
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        violations.append(name + ': unreviewed binary file')
        continue
    for label, pattern in text_rules:
        if pattern.search(text):
            violations.append(name + ': contains ' + label)
    if any(marker in text.casefold() for marker in retired_runtime_markers):
        violations.append(name + ': references the retired external runtime')
manifest = root / 'forge/manifest.yml'
if manifest.exists() and '00000000-0000-0000-0000-000000000000' not in manifest.read_text():
    violations.append('forge/manifest.yml: registered Forge app ID is not allowed')
if violations:
    raise SystemExit('Public-tree validation failed:\n' + '\n'.join(sorted(set(violations))))
print('Public-tree validation passed:', root)
