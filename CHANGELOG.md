# Changelog

## 0.2.5

- Set the FireFlow `Requestor` field to the Jira issue creator's email address while leaving `Owner` under FireFlow workflow control.
- Refuse submission with a clear mapping error when Jira does not expose a valid creator email, preventing incorrect requestor attribution.

## 0.2.4

- Preserve `tls_pin_only` in the runtime FireFlow transport so `doctor`, template reads, and traffic requests use the certificate trust mode selected during setup.

## 0.2.3

- Automatically discover every permitted FireFlow-supported ASMS device tree name during setup instead of requiring a comma-separated list.
- Validate FireFlow credentials before writing configuration and explain the mandatory first-login password change for new local ASMS users.
- Accept both current bare and legacy cookie-form `phpSessionId` authentication responses.
- Ensure preparation helpers use the newly installed release image instead of an older running container image.
- Allow up to 1000 discovered devices without changing the verified 100 traffic-line limit.

## 0.2.2

- Fixed Backspace (`BS` and `DEL`) and added `Ctrl-U` to clear interactive credential prompts, including hidden passwords and tokens.
- Validate ASMS administrator credentials before requesting new account details; report safe TLS, connection and authentication error categories.
- Allow retrying mismatched integration passwords without restarting account preparation.
- Clarified manual FireFlow account setup: configure username/password; the bus obtains and renews a temporary session instead of requiring a static API key.

## 0.2.1

- Added `bus_conf` with safe certificate-pin rotation and rollback.
- Added `prepare-fireflow.sh` and `prepare-jira.sh`; both run through the ready Docker image.
- Added opt-in pin-only TLS for IP/private-CA FireFlow deployments.
- Added unattended Docker installation from private `bus.json` and `secrets.json` files.
- Added optional private-CA staging while preserving hostname validation and certificate pinning.
- Run the new configuration doctor before stopping the existing container.
- Atomically replace container configuration and restore the previous deployment if startup fails.
- Added SELinux relabeling support for enforcing Rocky and RHEL hosts.

## 0.2.0

- Published a clean source snapshot without private operational history or deployment data.
- Removed automatic creation of broadly privileged ASMS accounts.
- Made the bus self-contained with direct Jira Cloud and FireFlow HTTPS API clients.
- Added a ready `linux/amd64` Docker image and a one-file Docker installer with embedded checksums.
- Added an SPDX SBOM, deterministic aggregate checksums, and a revision-bound release manifest.
- Made native installation offline and independent of package indexes.
- Accepted existing FireFlow object names in source and destination traffic items.
- Added fail-safe upgrade checks for legacy secret references and preserved receipt lookup.
- Bounded Jira intake scans and removed quadratic state lookups.
- Preserved immutable issue ID and canonical operator attribution for approval audit events.
- Added CodeQL, dependency review, secret scanning, Dependabot, and protected-branch checks.
