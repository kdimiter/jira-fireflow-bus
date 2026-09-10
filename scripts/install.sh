#!/bin/sh
# Install the Jira <-> FireFlow bus as a systemd service.
#
# Deliberately /bin/sh and POSIX: this has to run on Rocky 8 and on CentOS 7 without
# assuming which shell, python or package manager is present.
#
# It is idempotent. Run it again after an upgrade and it replaces the code and the units
# and leaves your configuration and your secrets exactly as they were. It NEVER overwrites
# /etc/algosec-jira-bus/bus.json or secrets.env, and it never enables writing: a fresh
# install polls in dry run until a person sets "apply": true in the configuration.
#
# The mode lives in the configuration and not in the unit precisely because this script
# replaces the unit. A mode kept in ExecStart would survive until the first upgrade and
# then quietly revert, and a bus back in dry run looks just like a bus with nothing to do.
#
#   ALGOSEC_CONNECTOR_SOURCE=/secure/connector.whl \
#   ALGOSEC_CONNECTOR_SHA256=EXPECTED_DIGEST ./scripts/install.sh  # internal install/upgrade
#   ./scripts/install.sh --uninstall     remove everything except config, secrets and state
#   ./scripts/install.sh --purge         remove those too
#
# After installing:
#   1. edit /etc/algosec-jira-bus/bus.json          (algosec-jira-bus fields helps)
#   2. put the two secrets in /etc/algosec-jira-bus/secrets.env
#   3. algosec-jira-bus --config ... doctor         until it reports no failures
#   4. systemctl start algosec-jira-bus.service     one dry-run pass; read the output
#   5. only then set "apply": true in bus.json -- NOT in the unit, which an upgrade replaces
set -eu
umask 077

NAME=algosec-jira-bus
PREFIX=/opt/$NAME
CONFIG_DIR=/etc/$NAME
STATE_DIR=/var/lib/$NAME
UNIT_DIR=/etc/systemd/system
CONNECTOR=${ALGOSEC_CONNECTOR_SOURCE:-}
CONNECTOR_SHA256=${ALGOSEC_CONNECTOR_SHA256:-}
SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: this creates a system account and writes to $UNIT_DIR"
[ -f "$SOURCE/pyproject.toml" ] || die "run this from a checkout of the bus repository"
# --- which systemd is this, and therefore which units ------------------------------------
# Not cosmetic. On systemd 219 the modern unit is broken rather than merely weaker:
# StateDirectory is unknown so the state directory is never created, and ProtectSystem=strict
# fails to parse and is silently ignored, so the file promises protection it never applies.
systemd_version() { systemctl --version 2>/dev/null | awk 'NR==1{print $2; exit}'; }
SYSTEMD=$(systemd_version || true)
# A systemctl binary alone (for example inside a regular Docker container) is not a
# running service manager. Refuse before creating users, directories or changing units.
systemctl show --property=Version --value >/dev/null 2>&1 \
    || die "systemd must be running and reachable; a systemctl executable alone is insufficient"
case "$SYSTEMD" in
    ''|*[!0-9]*) die "could not read the systemd version; refusing to guess which units to use" ;;
esac
if [ "$SYSTEMD" -ge 235 ]; then
    VARIANT=""      # packaging/systemd/<name>.service
    say "systemd $SYSTEMD: using the modern units"
else
    VARIANT=".centos7"
    say "systemd $SYSTEMD: using the CentOS 7 units (StateDirectory and ProtectSystem=strict"
    say "  are not available on this release; see the header of the unit for what that costs)"
fi

# --- uninstall ---------------------------------------------------------------------------
if [ "${1:-}" = "--uninstall" ] || [ "${1:-}" = "--purge" ]; then
    for unit in $NAME.timer $NAME-reconcile.timer; do
        systemctl disable --now "$unit" 2>/dev/null || true
    done
    rm -f "$UNIT_DIR/$NAME.service" "$UNIT_DIR/$NAME.timer" \
          "$UNIT_DIR/$NAME-reconcile.service" "$UNIT_DIR/$NAME-reconcile.timer"
    systemctl daemon-reload
    rm -rf "$PREFIX"
    if [ "${1:-}" = "--purge" ]; then
        # Config, secrets and state are the only things here a person cannot regenerate,
        # so removing them is a separate, explicit decision.
        rm -rf "$CONFIG_DIR" "$STATE_DIR"
        userdel "$NAME" 2>/dev/null || true
        groupdel "$NAME" 2>/dev/null || true
        say "removed, including configuration, secrets and state"
    else
        say "removed. Configuration, secrets and state kept in $CONFIG_DIR and $STATE_DIR"
        say "  (use --purge to remove those too)"
    fi
    exit 0
fi

# --- an interpreter new enough for the package -------------------------------------------
# The package needs 3.11. Rocky 8 ships one; CentOS 7's system python is 3.7, and there is
# no compiler on the appliances, so there the answer is a standalone build under /opt --
# which this script will not fetch for you, because downloading and trusting a runtime is a
# decision an operator should make deliberately.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 "$PREFIX/python/bin/python3" /opt/algosec-mcp/python/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
            PYTHON=$(command -v "$candidate"); break
        fi
    fi
done
[ -n "$PYTHON" ] || die "no python 3.11 or newer found.
  Rocky 8:   dnf install -y python3.11
  CentOS 7:  install a standalone build (python-build-standalone) under $PREFIX/python
             and run this again; there is no compiler on these appliances."
say "interpreter: $PYTHON ($("$PYTHON" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'))"
case "$PYTHON" in
    /opt/algosec-mcp/*)
        # A connector-owned standalone build can be reused deliberately. It is still
        # somebody else's interpreter -- removing or upgrading the connector would
        # take this venv with it. Fine as a deliberate choice on an appliance that already
        # has one; worth knowing rather than discovering.
        say "  note: this interpreter belongs to the connector's installation. Removing"
        say "  /opt/algosec-mcp would break this venv. Put a standalone build under"
        say "  $PREFIX/python if you want the bus to stand on its own."
        ;;
esac

# Used by the bundled bootstrap before stopping any existing polling services.
if [ "${1:-}" = "--preflight" ]; then
    "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
        || die "Python venv/ensurepip unavailable; install the matching Python venv package"
    say "preflight passed; no system changes made"
    exit 0
fi

# Installation executes wheel code as root. Verify and stage it here even when an operator
# invokes this internal script directly instead of using the public installer.
case "$CONNECTOR" in
    /*/algosec_host_mcp-*-py3-none-any.whl) ;;
    *) die "set ALGOSEC_CONNECTOR_SOURCE to an absolute authorized universal connector wheel" ;;
esac
[ -f "$CONNECTOR" ] && [ ! -L "$CONNECTOR" ] || die "connector wheel must be a regular file, not a symlink"
CONNECTOR_STAGE=$(mktemp -d /tmp/algosec-connector.XXXXXX)
trap 'rm -rf "$CONNECTOR_STAGE"' 0 HUP INT TERM
STAGED_CONNECTOR=$CONNECTOR_STAGE/$(basename "$CONNECTOR")
"$PYTHON" - "$CONNECTOR" "$CONNECTOR_SHA256" "$STAGED_CONNECTOR" <<'PY'
import hashlib, os, pathlib, stat, sys
source, expected, target = pathlib.Path(sys.argv[1]), sys.argv[2].lower(), pathlib.Path(sys.argv[3])
if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
    raise SystemExit('Connector SHA256 must be exactly 64 hexadecimal characters')
fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
digest = hashlib.sha256()
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > 100 * 1024 * 1024:
        raise SystemExit('Connector must be a regular file smaller than 100 MiB')
    with os.fdopen(fd, 'rb') as src, target.open('xb') as dst:
        fd = None
        for block in iter(lambda: src.read(1024 * 1024), b''):
            digest.update(block)
            dst.write(block)
finally:
    if fd is not None:
        os.close(fd)
if digest.hexdigest() != expected:
    target.unlink(missing_ok=True)
    raise SystemExit('Connector SHA256 mismatch; installation stopped')
target.chmod(0o600)
PY
CONNECTOR=$STAGED_CONNECTOR

# --- account and directories --------------------------------------------------------------
if ! getent group "$NAME" >/dev/null 2>&1; then groupadd --system "$NAME"; fi
if ! getent passwd "$NAME" >/dev/null 2>&1; then
    useradd --system --gid "$NAME" --home-dir "$PREFIX" --shell /sbin/nologin \
            --comment "AlgoSec FireFlow to Jira bus" "$NAME"
fi
install -d -m 0755 "$PREFIX"
install -d -m 0700 -o "$NAME" -g "$NAME" "$CONFIG_DIR"
install -d -m 0700 -o "$NAME" -g "$NAME" "$STATE_DIR"

# --- the code -----------------------------------------------------------------------------
if [ ! -x "$PREFIX/venv/bin/python" ]; then
    "$PYTHON" -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/python" -m pip install --quiet --upgrade pip
say "installing the operator-supplied connector wheel; public Python dependencies require package-index access"
"$PREFIX/venv/bin/python" -m pip install --quiet "$CONNECTOR" \
    || die "could not install the connector wheel or its public Python dependencies"
"$PREFIX/venv/bin/python" -m pip install --quiet "$SOURCE"
# The docs the units point at, so Documentation= is not a dangling path.
install -d -m 0755 "$PREFIX/docs"
for doc in "$SOURCE"/docs/*.md; do install -m 0644 "$doc" "$PREFIX/docs/"; done

# --- configuration, only if there is none yet ----------------------------------------------
if [ ! -f "$CONFIG_DIR/bus.json" ]; then
    install -m 0600 -o "$NAME" -g "$NAME" "$SOURCE/examples/jira-sync.json" "$CONFIG_DIR/bus.json"
    say "wrote a starting configuration to $CONFIG_DIR/bus.json -- every id in it is a"
    say "  placeholder and it will not work until you replace them"
else
    say "keeping the existing $CONFIG_DIR/bus.json"
fi
if [ ! -f "$CONFIG_DIR/secrets.env" ]; then
    umask 077
    cat > "$CONFIG_DIR/secrets.env" <<'ENV'
# Values for the env: references in bus.json. This file is read by systemd, not by a shell:
# write NAME=value with no quotes and no export.
#
# Use env: references here only where a keyring is unavailable, which is every headless
# appliance. On a host with a working keyring, prefer keyring: references in bus.json and
# leave this file empty.
#JIRA_API_TOKEN=
#ASMS_API_PASSWORD=
ENV
    chown "$NAME:$NAME" "$CONFIG_DIR/secrets.env"
    chmod 0600 "$CONFIG_DIR/secrets.env"
    say "wrote an empty $CONFIG_DIR/secrets.env (0600)"
else
    say "keeping the existing $CONFIG_DIR/secrets.env"
fi

# --- units ----------------------------------------------------------------------------------
for unit in "$NAME" "$NAME-reconcile"; do
    install -m 0644 "$SOURCE/packaging/systemd/${unit}${VARIANT}.service" "$UNIT_DIR/${unit}.service"
    install -m 0644 "$SOURCE/packaging/systemd/${unit}.timer" "$UNIT_DIR/${unit}.timer"
done
systemctl daemon-reload
# Enabled, not started: the timers will fire, and every pass is a dry run until somebody
# sets "apply": true in bus.json on purpose.
if [ "${ALGOSEC_SETUP_WIZARD:-0}" != 1 ]; then
    systemctl enable "$NAME.timer" "$NAME-reconcile.timer" >/dev/null
fi

say ""
say "installed. The bus is in DRY RUN: it will report what it would do and change nothing."
say ""
say "  1. edit    $CONFIG_DIR/bus.json"
say "             $PREFIX/venv/bin/$NAME --config $CONFIG_DIR/bus.json fields"
say "             lists your real Jira field ids"
say "  2. secrets $CONFIG_DIR/secrets.env"
say "  3. check   $PREFIX/venv/bin/$NAME --config $CONFIG_DIR/bus.json doctor"
say "             run it until it reports no failures"
say "  4. try     systemctl start $NAME.service && journalctl -u $NAME.service -n 50"
say "  5. only then set \"apply\": true in $CONFIG_DIR/bus.json"
say "             (in the configuration, not the unit -- an upgrade replaces the unit)"
