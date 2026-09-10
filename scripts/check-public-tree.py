#!/usr/bin/env python3
"""Fail closed when a candidate public tree contains known private material or binaries."""
from pathlib import Path
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
    if (name.endswith(forbidden_suffixes) or path.name in sensitive_names
            or name.endswith(sensitive_suffixes)
            or any(part in sensitive_directories for part in relative.parts)
            or 'docs/screenshots/' in name):
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
