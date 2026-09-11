"""One-shot ASMS account provisioning, isolated from the recurring bus."""
import getpass
import re
import socket
import ssl
import hashlib
import urllib.error
from urllib.parse import urlsplit

from .transport import https_origin, request_json
from .console import ConsoleError, prompt as _prompt


class ProvisionError(ValueError):
    """Safe fixed-text error which never contains credentials or response bodies."""


def capture_certificate_sha256(base_url):
    parsed = urlsplit(base_url)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=parsed.hostname) as connection:
            certificate = connection.getpeercert(binary_form=True)
    if not certificate:
        raise ProvisionError('ASMS did not present a TLS certificate')
    return hashlib.sha256(certificate).hexdigest()


def _request(config, path, **kwargs):
    timeout = config.get('request_timeout', 30)
    return request_json(config, path, timeout=timeout, **kwargs)


def _login_failure(error):
    """Categorize failures without displaying remote bodies or exception text."""
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (401, 403):
            return 'ASMS administrator login rejected (HTTP 401/403); check the administrator username and password.'
        code = error.code if type(error.code) is int else 'unexpected'
        return f'ASMS administrator login failed: unexpected HTTP {code}; check the ASMS API endpoint.'
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return ('ASMS TLS certificate verification failed; use --ca-file or '
                '--trust-server-certificate and verify the displayed fingerprint.')
    if isinstance(reason, ssl.SSLError):
        return 'ASMS TLS connection failed; check server TLS configuration and certificate.'
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'ASMS connection timed out; check connectivity and server availability.'
    if isinstance(reason, socket.gaierror):
        return 'ASMS DNS lookup failed; check the hostname and DNS from the Docker host.'
    if isinstance(reason, OSError) or isinstance(error, urllib.error.URLError):
        return 'ASMS connection failed; check connectivity, VPN and the HTTPS port.'
    return 'ASMS login returned an unexpected response; check the ASMS API endpoint and version.'


def _connection(transport):
    parsed = urlsplit(transport.get('base_url', ''))
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ProvisionError('ASMS base URL must be an HTTPS origin')
    return {key: value for key, value in transport.items()
            if key in ('base_url', 'ca_file', 'tls_certificate_sha256',
                       'tls_pin_only', 'request_timeout')}


def _validate_inputs(*values):
    for value in values:
        if (not isinstance(value, str) or not value
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
            raise ProvisionError('Account inputs must be nonempty strings without control characters')


def _validate_session(session):
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', session):
        raise ProvisionError('ASMS administrator login did not return a valid session')
    return session


def authenticate_asms(transport, admin_username, admin_password, request=None):
    """Verify administrator access without creating or modifying a user."""
    connection = _connection(transport)
    _validate_inputs(admin_username, admin_password)
    request = request or _request
    try:
        auth = request(connection, '/fa/server/connection/login', method='POST',
                       body={'username': admin_username, 'password': admin_password})
    except Exception as error:
        raise ProvisionError(_login_failure(error)) from None
    if isinstance(auth, dict) and auth.get('status') is False:
        raise ProvisionError('ASMS administrator credentials rejected; check the username and password.')
    if not isinstance(auth, dict) or auth.get('status') is not True:
        raise ProvisionError('ASMS administrator login did not return a valid session')
    return _validate_session(auth.get('SessionID'))


def create_asms_user(transport, admin_username, admin_password, username, password,
                     email, request=None, *, _session=None):
    """Create a local ASMS/FireFlow admin with ALL_FIREWALLS Standard access."""
    connection = _connection(transport)
    _validate_inputs(admin_username, admin_password, username, password, email)
    if username == admin_username:
        raise ProvisionError('Use a dedicated integration username, not the administrator')
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', username):
        raise ProvisionError('Invalid integration username')
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        raise ProvisionError('Invalid integration email')
    request = request or _request
    session = (authenticate_asms(connection, admin_username, admin_password, request)
               if _session is None else _validate_session(_session))
    body = {
        'userName': username,
        'password': password,
        'adminPassword': admin_password,
        'fullName': 'AlgoSec Jira Integration',
        'email': email,
        'authenticationType': 'local',
        'administrator': 'yes',
        'fireflowAdmin': 'yes',
        'landingPage': 'aff',
        'firewallProfile': 'Standard',
        'authorizedDevices': [{
            'id': 'ALL_FIREWALLS', 'displayName': 'ALL_FIREWALLS',
            'profile': 'Standard', 'notification': 'no'}],
    }
    try:
        reply = request(connection, '/afa/api/v1/users', method='POST', body=body,
                        headers={'Cookie': 'PHPSESSID=' + session})
    except Exception:
        raise ProvisionError('ASMS user creation failed or outcome is unknown; inspect Users before retrying') from None
    users = reply.get('successUsers') if isinstance(reply, dict) else None
    if (not isinstance(reply, dict) or reply.get('status') != 'OK'
            or not isinstance(users, list)
            or not any(isinstance(user, dict) and user.get('username') == username
                       for user in users)):
        raise ProvisionError('ASMS did not confirm user creation; inspect Users before retrying')
    return {'username': username, 'created': True, 'runtime_login_verified': False,
            'administrator': True, 'fireflow_admin': True,
            'device_profile': 'ALL_FIREWALLS:Standard'}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description='Create the dedicated ASMS/FireFlow integration account')
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--ca-file')
    parser.add_argument('--tls-certificate-sha256')
    parser.add_argument('--trust-server-certificate', action='store_true')
    parser.add_argument('--apply', action='store_true',
                        help='Required confirmation that one user may be created')
    args = parser.parse_args(argv)
    if not args.apply:
        print('Dry run: add --apply to create one ASMS/FireFlow integration user.')
        return 0
    transport = {'base_url': https_origin(args.base_url)}
    if args.ca_file:
        transport['ca_file'] = args.ca_file
    if args.tls_certificate_sha256:
        transport['tls_certificate_sha256'] = args.tls_certificate_sha256.lower()
    if args.trust_server_certificate:
        if not args.tls_certificate_sha256:
            fingerprint = capture_certificate_sha256(transport['base_url'])
            print('ASMS certificate SHA256:', fingerprint)
            if _prompt('Type TRUST to pin this exact certificate: ').strip() != 'TRUST':
                print('No changes made.')
                return 2
            transport['tls_certificate_sha256'] = fingerprint
        transport['tls_pin_only'] = True
    admin = _prompt('Existing ASMS administrator username: ').strip()
    admin_password = _prompt('Existing ASMS administrator password: ', secret=True)
    session = authenticate_asms(transport, admin, admin_password)
    print('ASMS administrator login verified.')
    username = _prompt('New integration username [jira_bus_api]: ').strip() or 'jira_bus_api'
    email = _prompt('Unique integration email: ').strip()
    for attempt in range(3):
        password = _prompt('New integration password (masked): ', secret=True)
        repeated = _prompt('Repeat new integration password: ', secret=True)
        if password == repeated:
            break
        print('Passwords do not match; enter the new password again.')
    else:
        print('Three password attempts failed; no changes made.')
        return 2
    if _prompt('Type CREATE to grant ASMS Admin, FireFlow Admin and ALL_FIREWALLS Standard: ').strip() != 'CREATE':
        print('No changes made.')
        return 2
    create_asms_user(
        transport, admin, admin_password, username, password, email, _session=session)
    print('Account created. Enter the same password later in bus_conf.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ProvisionError, ConsoleError) as error:
        print('FireFlow preparation stopped:', str(error))
        raise SystemExit(1)
