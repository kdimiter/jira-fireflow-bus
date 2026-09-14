#!/usr/bin/env python3
"""Interactive Linux setup. Never submits a firewall change during setup."""
import argparse
import copy
import getpass
import json
import os
from pathlib import Path
import pwd
import re
import socket
import ssl
import subprocess
import sys
import stat
import tempfile
import uuid
from urllib.parse import urlsplit

from algosec_jira_bus.config import private_json
from algosec_jira_bus.console import prompt


MAX_PRIVATE_TEXT_BYTES = 1024 * 1024
DEFAULT_JIRA_STATUS_MAPS = {
    'full': {
        'To Do': 'open', 'Done': 'resolved', 'Rejected': 'rejected',
        'Cancelled': 'cancelled',
    },
    'compact': {
        'To Do': 'open', 'Done': 'resolved',
        'Rejected / Cancelled': 'cancelled',
    },
}


def workflow_template_path(source, profile):
    """Resolve a packaged workflow template from a validated profile name."""
    names = {
        'full': 'jira-sync-basic-structured.json',
        'compact': 'jira-sync-basic-structured-compact.json',
    }
    if profile not in names:
        raise ValueError('Unknown Jira workflow profile')
    return Path(source) / 'examples' / names[profile]


def default_jira_status_map(settings):
    profile = settings.get('workflow_profile')
    if profile is None:
        transitions = settings.get('mirror', {}).get('transitions', {})
        profile = ('compact' if isinstance(transitions, dict)
                   and 'Rejected / Cancelled' in transitions.values() else 'full')
    if profile not in DEFAULT_JIRA_STATUS_MAPS:
        raise ValueError('Unknown Jira workflow profile in configuration')
    return dict(DEFAULT_JIRA_STATUS_MAPS[profile])


def validate_secret_references(settings):
    """Require references supported by the self-contained headless runtime."""
    references = []
    if isinstance(settings, dict):
        jira = settings.get('jira')
        fireflow = settings.get('fireflow')
        if isinstance(jira, dict) and 'token_ref' in jira:
            references.append(('jira.token_ref', jira.get('token_ref')))
        if isinstance(fireflow, dict):
            references.extend((('fireflow.' + name, fireflow.get(name))
                               for name in ('password_ref', 'session_ref')
                               if name in fireflow))
    incompatible = [name for name, value in references
                    if not isinstance(value, str)
                    or not re.fullmatch(r'env:[A-Za-z_][A-Za-z0-9_]{0,127}', value)]
    if incompatible:
        raise ValueError('Existing configuration uses unsupported secret references (%s). '
                         'Before upgrading, move those secrets to the private secrets file '
                         'and change each reference to env:NAME.' % ', '.join(incompatible))


def _read_text(path, *, max_bytes, expected_uid=None):
    if (type(max_bytes) is not int or max_bytes < 1 or
            (expected_uid is not None and type(expected_uid) is not int)):
        raise ValueError('Invalid file reader limits')
    flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) |
             getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        descriptor = os.open(Path(path), flags)
    except FileNotFoundError:
        raise
    except OSError:
        raise ValueError('Expected an owner-only regular file') from None
    try:
        information = os.fstat(descriptor)
        if (not stat.S_ISREG(information.st_mode) or information.st_nlink != 1 or
                (expected_uid is not None and
                 (information.st_uid != expected_uid or information.st_mode & 0o077))):
            raise ValueError('Expected a safe regular file')
        if information.st_size > max_bytes:
            raise ValueError('Private file is too large')
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        if len(raw) > max_bytes:
            raise ValueError('Private file is too large')
    finally:
        os.close(descriptor)
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Private file must contain valid UTF-8 text') from None


def read_private_text(path, expected_uid, *, max_bytes=MAX_PRIVATE_TEXT_BYTES):
    """Read a bounded owner-only regular file through a no-follow descriptor."""
    return _read_text(path, max_bytes=max_bytes, expected_uid=expected_uid)


def read_regular_text(path, *, max_bytes=MAX_PRIVATE_TEXT_BYTES):
    """Read a bounded non-link source file without requiring private permissions."""
    return _read_text(path, max_bytes=max_bytes)


def ask(label, default=''):
    value = prompt(f'{label}' + (f' [{default}]' if default else '') + ': ').strip()
    return value or default


def ask_boolean(label, default):
    value = ask(label + (' (Y/n)' if default else ' (y/N)'),
                'Y' if default else 'N').casefold()
    if value in ('y', 'yes'):
        return True
    if value in ('n', 'no'):
        return False
    raise ValueError('Answer y or n')


def status_mapping(value):
    """Parse the editable menu value without accepting RT field separators."""
    result = {}
    folded = set()
    for item in value.split(','):
        if '=' not in item:
            raise ValueError('Use Jira status=FireFlow status pairs separated by commas')
        jira_status, fireflow_status = (part.strip() for part in item.split('=', 1))
        key = jira_status.casefold()
        if (not jira_status or not fireflow_status or key in folded
                or any(char in jira_status + fireflow_status for char in '\r\n\x00')
                or fireflow_status not in ('open', 'resolved', 'cancelled', 'rejected')):
            raise ValueError('Invalid or duplicate Jira to FireFlow status mapping')
        folded.add(key)
        result[jira_status] = fireflow_status
    return result


def configure_jira_to_fireflow(config, settings, account):
    """Configure reverse synchronization without asking for either API secret again."""
    current = settings.get('jira_to_fireflow')
    if current is None:
        current = {}
    if not isinstance(current, dict):
        raise ValueError('jira_to_fireflow must be an object')
    enabled = ask_boolean('Enable Jira to FireFlow synchronization?',
                          current.get('enabled', True) is not False)
    if enabled:
        comments = ask_boolean('Copy new Jira comments to FireFlow History?',
                               current.get('comments', True) is not False)
        statuses = ask_boolean('Apply mapped Jira workflow changes in FireFlow?',
                               bool(current.get('status_map', True)))
        existing_map = current.get('status_map')
        status_map = (dict(existing_map) if statuses and isinstance(existing_map, dict)
                      and existing_map else default_jira_status_map(settings)
                      if statuses else {})
        if statuses:
            default_map = ', '.join('%s=%s' % item for item in status_map.items())
            status_map = status_mapping(ask(
                'Jira to FireFlow status map (Jira=FireFlow, comma-separated)',
                default_map))
        print('Jira status mapping:', ', '.join(
            '%s -> %s' % item for item in status_map.items()) if status_map else 'disabled')
    else:
        comments = current.get('comments', True) is not False
        status_map = current.get('status_map')
        if not isinstance(status_map, dict):
            status_map = default_jira_status_map(settings)
    settings['jira_to_fireflow'] = {
        'enabled': enabled,
        'comments': comments,
        'status_map': status_map,
    }
    fireflow = settings.get('fireflow')
    if not isinstance(fireflow, dict):
        raise ValueError('FireFlow configuration is missing')
    fireflow['legacy_rt_enabled'] = enabled
    write_private(config, json.dumps(settings, indent=2, ensure_ascii=False) + '\n',
                  account.pw_uid, account.pw_gid)
    print('Jira to FireFlow synchronization %s. Existing credentials and state were preserved.'
          % ('enabled' if enabled else 'disabled'))
    return 0


def origin(value):
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.path not in ('', '/') or p.query or p.fragment:
        raise ValueError('Enter an HTTPS origin without path, credentials or query')
    return value.rstrip('/')


def secret_line(name, value):
    if not re.fullmatch(r'[A-Z_][A-Z0-9_]*', name):
        raise ValueError('Invalid environment variable name')
    if not value or any(c in value for c in '\r\n\x00'):
        raise ValueError('Secret must be nonempty and single-line')
    # systemd EnvironmentFile double-quoted syntax, not shell evaluation.
    return name + '="' + value.replace('\\', '\\\\').replace('"', '\\"') + '"\n'


def write_private(path, content, uid, gid):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing symlink: ' + str(path))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            os.fchown(f.fileno(), uid, gid)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def choose_field(fields, name, kind):
    candidates = [f for f in fields if f.get('name') == name and f.get('schema', {}).get('type') == kind]
    if len(candidates) == 1:
        field = candidates[0]['id']
        print(f'{name}: {field}')
        return field
    print(f'Select {name}; expected type={kind}:')
    for f in fields:
        if f.get('schema', {}).get('type') == kind:
            print(f"  {f['id']}: {f.get('name', '')}")
    value = ask('Field ID')
    if not any(f.get('id') == value and f.get('schema', {}).get('type') == kind for f in fields):
        raise ValueError('Field not found or wrong type. Configure Jira then rerun.')
    return value


def choose_structured_field(fields, forge_app_id=None):
    """Use the production field for the app selected by guided provisioning."""
    if forge_app_id is None:
        return choose_field(fields, 'Мережеві доступи AlgoSec', 'object')
    from algosec_jira_bus.jira_provision import (
        JiraProvisionError, _structured_forge_field)
    try:
        field = _structured_forge_field(fields, forge_app_id)
    except JiraProvisionError as error:
        raise ValueError(str(error)) from None
    if field.get('schema', {}).get('type') != 'object':
        raise ValueError('Selected Forge field has the wrong Jira type')
    print('Мережеві доступи AlgoSec:', field['id'])
    return field['id']


def validate_worktype(project_data, worktype):
    if not any(str(t.get('id')) == worktype and not t.get('subtask', False) for t in project_data.get('issueTypes', [])):
        raise ValueError('Work type must belong to the selected project and cannot be a subtask')


def build_config(template, jira_url, email, project, issue_type, ff_url, ff_user,
                 devices, pin, fields, *, trust_server_certificate=False,
                 existing_ca_file=None):
    if not re.fullmatch('[A-Z][A-Z0-9_]{1,20}', project):
        raise ValueError('Invalid project key')
    if not issue_type.isdecimal():
        raise ValueError('Use numeric Jira work type ID')
    if (not isinstance(devices, list) or not devices or len(devices) > 1000
            or any(not isinstance(d, str) or not d or len(d) > 4096
                   or d != d.strip()
                   or any(ord(char) < 32 or 127 <= ord(char) <= 159
                          for char in d)
                   for d in devices)):
        raise ValueError('Device tree names are required')
    if pin and not re.fullmatch('[0-9a-fA-F]{64}', pin):
        raise ValueError('Certificate SHA256 must be 64 hex characters')
    if type(trust_server_certificate) is not bool:
        raise ValueError('Trust server certificate must be true or false')
    if trust_server_certificate and (not pin or pin.lower() == '0' * 64):
        raise ValueError('Trust server certificate requires a captured certificate SHA256')
    if not email.strip() or not ff_user.strip():
        raise ValueError('API email and FireFlow username are required')
    if set(fields) != {'structured', 'id', 'status', 'owner'} or any(not re.fullmatch(r'customfield_[0-9]+', v) for v in fields.values()) or len(set(fields.values())) != 4:
        raise ValueError('Four distinct Jira custom field IDs are required')
    c = copy.deepcopy(template)
    c['jira'].update(base_url=origin(jira_url), email=email,
                     jql=f'project = {project} AND issuetype = {issue_type} AND status = "To Do" ORDER BY created ASC')
    c['fireflow'].update(base_url=origin(ff_url), username=ff_user,
                         devices=devices, allowed_devices=devices)
    c['fireflow'].pop('tls_certificate_sha256', None)
    c['fireflow'].pop('tls_pin_only', None)
    c['fireflow'].pop('ca_file', None)
    if pin:
        c['fireflow']['tls_certificate_sha256'] = pin.lower()
    if trust_server_certificate:
        c['fireflow']['tls_pin_only'] = True
    elif existing_ca_file:
        c['fireflow']['ca_file'] = existing_ca_file
    c['mapping']['structured']['field'] = fields['structured']
    c['mirror']['result_fields'] = {k: fields[k] for k in ('id', 'status', 'owner')}
    c['apply'] = False
    return c


def capture_certificate_sha256(ff_url, timeout=10):
    """Capture the presented leaf certificate for explicit administrator trust."""
    parsed = urlsplit(origin(ff_url))
    host, port = parsed.hostname, parsed.port or 443
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if hasattr(ssl, 'OP_NO_COMPRESSION'):
        context.options |= ssl.OP_NO_COMPRESSION
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as connection:
            certificate = connection.getpeercert(binary_form=True)
    if not certificate:
        raise ValueError('ASMS did not present a TLS certificate')
    import hashlib
    return hashlib.sha256(certificate).hexdigest()


def collect_fireflow_credentials():
    """Collect an existing dedicated integration account."""
    ff_url = origin(ask('ASMS URL'))
    trust = ask('Trust server certificate? [y/N]', 'N').casefold() in ('y', 'yes')
    pin = ''
    if trust:
        pin = capture_certificate_sha256(ff_url)
        print('ASMS certificate SHA256:', pin)
        if ask('Type TRUST to pin this exact certificate') != 'TRUST':
            raise ValueError('Server certificate was not trusted')
    ff_user = ask('FireFlow username', 'jira_bus_api')
    password = prompt('Existing FireFlow password: ', secret=True)
    return ff_url, ff_user, password, pin, trust


def discover_fireflow_devices(ff_url, ff_user, password, pin,
                              trust_server_certificate, ca_file=None):
    """Validate FireFlow credentials and load all permitted device tree names."""
    from algosec_jira_bus.provision import list_fireflow_device_tree_names
    transport = {'base_url': origin(ff_url)}
    if trust_server_certificate:
        transport['tls_certificate_sha256'] = pin.lower()
        transport['tls_pin_only'] = True
    elif ca_file:
        transport['ca_file'] = ca_file
    return list_fireflow_device_tree_names(transport, ff_user, password)


def run_doctor(config):
    """Use a runtime oneshot unit, supported by systemd 219 and later."""
    unit = 'algosec-jira-bus-setup-' + uuid.uuid4().hex + '.service'
    path = Path('/run/systemd/system') / unit
    # Config is the fixed installer path. Reject unit syntax interpolation.
    if str(config) != '/etc/algosec-jira-bus/bus.json':
        raise ValueError('Unexpected doctor configuration path')
    content = ('[Unit]\nDescription=AlgoSec setup connectivity check\n'
               '[Service]\nType=oneshot\nUser=algosec-jira-bus\nGroup=algosec-jira-bus\n'
               'EnvironmentFile=/etc/algosec-jira-bus/secrets.env\nUMask=0077\n'
               'ExecStart=/opt/algosec-jira-bus/venv/bin/algosec-jira-bus '
               '--config /etc/algosec-jira-bus/bus.json --state-dir /var/lib/algosec-jira-bus doctor\n')
    write_private(path, content, 0, 0)
    try:
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        # Type=oneshot start waits for the command and returns failure on doctor failure.
        result = subprocess.run(['systemctl', 'start', unit])
        subprocess.run(['journalctl', '--no-pager', '-u', unit])
        return result
    finally:
        path.unlink(missing_ok=True)
        subprocess.run(['systemctl', 'reset-failed', unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)


def finish_setup(config, settings, account):
    """Recheck saved setup on every run before allowing activation."""
    check = run_doctor(config)
    if check.returncode:
        print('NOT READY: doctor failed. Configuration saved; no timer started. Fix reported issues and run doctor again.')
        return 1
    print('Connectivity doctor passed; this is not an end-to-end integration test. Confirm Jira workflow, Forge required field, and API account rights using the guide.')
    if ask('Type START to enable synchronization; Enter keeps dry run') != 'START':
        print('No activation requested; existing apply setting and service state preserved.')
        return 0
    previous_apply = settings.get('apply', False)
    settings['apply'] = True
    dropin = Path('/etc/systemd/system/algosec-jira-bus.timer.d')
    dropin.mkdir(exist_ok=True)
    (dropin / 'interval.conf').write_text('[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=30s\nAccuracySec=1s\n')
    try:
        write_private(config, json.dumps(settings, indent=2, ensure_ascii=False) + '\n', account.pw_uid, account.pw_gid)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', '--now', 'algosec-jira-bus.timer', 'algosec-jira-bus-reconcile.timer'], check=True)
    except (OSError, subprocess.CalledProcessError):
        settings['apply'] = previous_apply
        write_private(config, json.dumps(settings, indent=2, ensure_ascii=False) + '\n', account.pw_uid, account.pw_gid)
        print('Activation failed; prior apply setting restored. Inspect both timers before retrying.')
        raise
    print('Synchronization enabled, 30-second poll. Check journalctl -u algosec-jira-bus.service.')
    return 0


def finish_container(config, settings, account):
    """Validate inside an isolated container; activation only changes saved apply."""
    secrets = private_json(config.parent / 'secrets.json', account.pw_uid,
                           max_bytes=64 * 1024)
    if set(secrets) != {'JIRA_API_TOKEN', 'ASMS_API_PASSWORD'} or any(
            not isinstance(v, str) or not v or any(c in v for c in '\r\n\x00') for v in secrets.values()):
        raise ValueError('Invalid container secrets.json')
    env = dict(os.environ, **secrets)
    result = subprocess.run([sys.executable, '-m', 'algosec_jira_bus.bus',
                             '--config', str(config), '--state-dir', '/var/lib/algosec-jira-bus',
                             'doctor'], env=env)
    if result.returncode:
        print('NOT READY: doctor failed; container activation refused.')
        return 1
    settings['apply'] = ask('Type START to enable synchronization; Enter saves dry run') == 'START'
    write_private(config, json.dumps(settings, indent=2, ensure_ascii=False) + '\n', account.pw_uid, account.pw_gid)
    print('Configuration validated. Container helper may now start the service.')
    return 0


def refresh_container_certificate(config, settings, account):
    """Rotate a pin-only FireFlow certificate without re-entering API secrets."""
    fireflow = settings.get('fireflow') if isinstance(settings, dict) else None
    if not isinstance(fireflow, dict) or not fireflow.get('base_url'):
        raise ValueError('FireFlow URL is not configured')
    pin = capture_certificate_sha256(fireflow['base_url'])
    print('New ASMS certificate SHA256:', pin)
    if ask('Type TRUST to replace the pinned server certificate') != 'TRUST':
        raise ValueError('New server certificate was not trusted')
    fireflow['tls_certificate_sha256'] = pin
    fireflow['tls_pin_only'] = True
    fireflow.pop('ca_file', None)
    write_private(config, json.dumps(settings, indent=2, ensure_ascii=False) + '\n',
                  account.pw_uid, account.pw_gid)
    secrets = private_json(config.parent / 'secrets.json', account.pw_uid,
                           max_bytes=64 * 1024)
    env = dict(os.environ, **secrets)
    result = subprocess.run([
        sys.executable, '-m', 'algosec_jira_bus.bus', '--config', str(config),
        '--state-dir', '/var/lib/algosec-jira-bus', 'doctor'], env=env)
    if result.returncode:
        print('NOT READY: doctor rejected the new certificate.')
        return 1
    print('New FireFlow certificate pinned and connectivity doctor passed.')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--container', action='store_true', help='Configure mounted files without systemd')
    parser.add_argument('--reconfigure', action='store_true',
                        help='Replace an existing configuration after revalidating both APIs')
    parser.add_argument('--refresh-certificate', action='store_true',
                        help='Capture and validate a replacement FireFlow certificate pin')
    parser.add_argument('--jira-sync', action='store_true',
                        help='Configure Jira to FireFlow status and comment synchronization')
    parser.add_argument('--forge-app-id',
                        help='use the selected production Forge app field')
    parser.add_argument('--workflow-profile', choices=('full', 'compact'))
    args = parser.parse_args()
    if sys.platform != 'linux' or (not args.container and os.geteuid() != 0):
        raise ValueError('Run on Linux as root')
    account = pwd.getpwuid(os.geteuid()) if args.container else pwd.getpwnam('algosec-jira-bus')
    finish = finish_container if args.container else finish_setup
    folder = Path('/etc/algosec-jira-bus')
    config = folder / 'bus.json'
    print('AlgoSec Jira integration setup. Secrets are not printed.')
    print('Guide: https://github.com/kdimiter/jira-fireflow-bus/blob/main/docs/DEPLOYMENT-GUIDE-uk.md')
    print('Jira: deploy Forge and run prepare-jira.sh before field discovery.')
    print('Dedicated Jira and ASMS API accounts must already be prepared.')
    print('Grant only the rights validated for this deployment; the wizard does not grant roles.')
    print('Use a Jira token supported by this tenant-origin client; scoped gateway tokens are not supported by this wizard.')
    try:
        existing = private_json(config, account.pw_uid)
    except FileNotFoundError:
        existing = {}
    selected_workflow_profile = (args.workflow_profile
                                 or existing.get('workflow_profile') or 'full')
    if selected_workflow_profile not in DEFAULT_JIRA_STATUS_MAPS:
        raise ValueError('Unknown Jira workflow profile in existing configuration')
    validate_secret_references(existing)
    configured = existing.get('mapping', {}).get('structured') and 'YOUR-' not in existing.get('jira', {}).get('base_url', '')
    if args.refresh_certificate:
        if not args.container or not configured:
            raise ValueError('Certificate refresh requires an existing container configuration')
        return refresh_container_certificate(config, existing, account)
    if args.jira_sync:
        if not args.container or not configured:
            raise ValueError('Jira to FireFlow setup requires an existing container configuration')
        return configure_jira_to_fireflow(config, existing, account)
    if configured and not args.reconfigure:
        print('Existing configuration detected; preserving it and secrets.')
        return finish(config, existing, account)
    secrets_path = folder / ('secrets.json' if args.container else 'secrets.env')
    try:
        existing_secrets = read_private_text(secrets_path, account.pw_uid,
                                             max_bytes=64 * 1024)
    except FileNotFoundError:
        existing_secrets = ''
    if not args.reconfigure and any(line.strip() and not line.lstrip().startswith('#')
                                    for line in existing_secrets.splitlines()):
        raise ValueError('Existing secrets.env preserved. Complete bus.json using the guide, then rerun; wizard will not replace existing credentials.')
    url = origin(ask('Jira site URL'))
    email = ask('Jira API account email')
    token = prompt('Jira API token: ', secret=True)
    os.environ['JIRA_API_TOKEN'] = token
    from algosec_jira_bus.jira import Jira
    jira = Jira({'base_url': url, 'email': email, 'token_ref': 'env:JIRA_API_TOKEN'})
    print('Jira identity:', jira.myself().get('displayName'))
    project = ask('Project key', 'NET')
    project_data = jira._call('/rest/api/3/project/' + project) if re.fullmatch('[A-Z][A-Z0-9_]{1,20}', project) else {}
    print('Work types:', ', '.join(f"{t['id']}={t['name']}" for t in project_data.get('issueTypes', [])))
    worktype = ask('Network Access work type numeric ID')
    validate_worktype(project_data, worktype)
    available = jira.fields()
    fields = {'structured': choose_structured_field(available, args.forge_app_id)}
    for k, name in [('id', 'FireFlow Request ID'), ('status', 'FireFlow Status'), ('owner', 'FireFlow Owner')]:
        fields[k] = choose_field(available, name, 'string')
    existing_ca_file = (existing.get('fireflow', {}).get('ca_file')
                        if isinstance(existing.get('fireflow'), dict) else None)
    ff_url, ff_user, password, pin, trust_server_certificate = collect_fireflow_credentials()
    devices = discover_fireflow_devices(
        ff_url, ff_user, password, pin, trust_server_certificate,
        existing_ca_file)
    print('FireFlow-supported device tree names discovered:', len(devices))
    template = json.loads(read_regular_text(
        workflow_template_path(args.source, selected_workflow_profile)))
    settings = build_config(
        template, url, email, project, worktype, ff_url, ff_user, devices, pin,
        fields, trust_server_certificate=trust_server_certificate,
        existing_ca_file=existing_ca_file)
    settings['workflow_profile'] = selected_workflow_profile
    secrets = (json.dumps({'JIRA_API_TOKEN': token, 'ASMS_API_PASSWORD': password}) + '\n' if args.container
               else secret_line('JIRA_API_TOKEN', token) + secret_line('ASMS_API_PASSWORD', password))
    # Preserve original installer placeholders, too; never replace silently on a rerun.
    for p in (config, secrets_path):
        try:
            original = read_private_text(p, account.pw_uid)
        except FileNotFoundError:
            continue
        backup = p.with_suffix(p.suffix + '.before-wizard')
        try:
            backup.lstat()
        except FileNotFoundError:
            write_private(backup, original, account.pw_uid, account.pw_gid)
    write_private(secrets_path, secrets, account.pw_uid, account.pw_gid)
    write_private(config, json.dumps(settings, indent=2, ensure_ascii=False) + '\n', account.pw_uid, account.pw_gid)
    return finish(config, settings, account)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print('Setup stopped:', str(e), file=sys.stderr)
        raise SystemExit(1)
