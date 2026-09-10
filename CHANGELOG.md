# Changelog

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
