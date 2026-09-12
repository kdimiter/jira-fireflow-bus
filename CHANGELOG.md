# Changelog

## 0.3.12

- Persist valid Forge field drafts directly through the Jira submit bridge in create-like dialogs and keep standard blur submission enabled, so Jira validates the current structured value when Create is clicked.
- Load the edit context once through `view.getContext()` to avoid stale or ambiguous UI Kit context shapes.

## 0.3.11

- Read the Jira UI Kit custom-field context from both supported context shapes so the create
  dialog enables blur submission and sends the completed structured value to Jira validation.
- Let the guided installer either reuse an existing production Forge app unchanged or deploy
  the bundled update to that same App ID with `forge install --upgrade`, without registering a
  duplicate app.

## 0.3.10

- Let the guided Linux installer discover compatible Forge apps already installed in the
  selected Jira site and offer them alongside a create-new option.
- Skip Node.js, Forge CLI, deployment, installation, and Forge credentials when an existing
  production app is selected; reuse temporary Jira administrator credentials only for
  discovery and Jira preparation.
- Pin Jira preparation to the selected Forge App ID and prefer its production field when
  development and production copies coexist.

## 0.3.9

- Make `prepare-jira.sh` create and verify the complete Basic Change Traffic Request
  workflow with `To Do`, `Plan`, `Approve`, `Implement`, `Validate`, `Match`, `Done`,
  `Rejected`, and `Cancelled` statuses and matching global transitions.
- Give the selected standard work type dedicated work-type, workflow, screen, and field
  configuration schemes without changing shared Jira defaults or other work types.
- Keep provisioning idempotent, refuse automatic migration of a nonempty Space, and read
  every generated scheme and workflow back before reporting success.
- Align fresh Jira-to-FireFlow and FireFlow-to-Jira mappings with the Basic workflow; the
  Multi-/Parallel-Approval-only `Review` stage is no longer requested by Basic installs.
- Validate the installer against a real Jira Cloud Space, Forge form, synthetic request,
  and all nine available workflow transitions.

## 0.3.8

- Add one `--guided` Linux workflow with terminal dialog windows for host prerequisites,
  Forge registration or reuse, Jira Space/work type preparation, and Docker bus setup.
- Bundle the complete Forge application inside the verified Docker installer, so a target
  Linux server no longer needs a repository checkout or a Mac deployment workstation.
- Preserve an existing registered Forge App ID during code refresh and refuse replacement
  with another App ID, preventing duplicate Jira fields during reruns.
- Scope the Forge requirement through the selected work type's field configuration, allowing
  other native Jira work types in the same Space without a hidden Forge validation failure.
- Install Node.js 22 through a per-user nvm directory and run Forge without root privileges;
  setup tokens are masked, held only for the deployment stage, and removed afterward.
- Give every prepared company-managed Jira Space its own field configuration: the Forge
  request field is visible and required, while FireFlow ID, status and owner stay visible
  and optional without changing Jira's shared Default Field Configuration.

## 0.3.7

- Let Jira preparation prompt for the Space key, Space name, and work type name when they
  are not supplied as command-line options.
- Add `--work-type-name` so deployments can select a unique standard work type instead of
  failing when the tenant contains duplicate `Network Access` names.

## 0.3.6

- Accept Jira Cloud project-name validation responses returned as either a documented JSON
  string or an unquoted plain-text scalar, while keeping all other Jira JSON decoding strict.
- Report malformed Jira provisioning responses as a concise setup error instead of a Python
  traceback.

## 0.3.5

- Create the FireFlow request without a `Requestor` field when Jira hides or omits the
  creator email; a missing email no longer refuses or retries an otherwise valid request.
- Keep sending a validated Jira creator email as FireFlow `Requestor` when it is available.
- Remove the optional FireFlow Owner to Jira Assignee mapping introduced in the superseded
  0.3.4 prerelease.

## 0.3.3

- Request Jira creator and reporter fields during the installer connectivity doctor so
  valid Requestor attribution is checked from the same issue data used by live polling.

## 0.3.2

- Preserve Jira creator and reporter fields in the fresh approval-verified snapshot so
  FireFlow Requestor attribution uses the actual Jira ticket creator.

## 0.3.1

- Add an idempotent `create-jira-space.sh` helper for validating, creating, and reusing a
  company-managed Jira Space before the Forge app and remaining Jira integration objects are
  prepared.

## 0.3.0

- Add opt-in Jira-to-FireFlow synchronization for new internal Jira comments and explicitly mapped Jira workflow transitions.
- Mark FireFlow-originated Jira comments and transitions so the reverse direction does not echo them back.
- Baseline existing Jira history on first activation and persist immutable event cursors and operation receipts to prevent replay.
- Add `sudo bus_conf --jira-sync` to enable, disable, and edit the status map without re-entering API credentials.
- Use the FireFlow RT REST compatibility API only behind an explicit configuration gate; verify comments in History and status changes by a new status transaction.
- Preserve existing secrets, state, and disabled reverse-sync defaults during an installer-managed upgrade.

## 0.2.6

- Add a non-interactive `--upgrade` path that keeps the existing secrets and state, migrates only non-secret configuration, validates the new image before downtime, and restores the previous container and configuration if readiness fails.
- Install `bus_update` so later Docker releases can be applied from a downloaded `.run` and checksum pair without reopening the credential wizard.

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
