# Security policy

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** function in the repository Security tab. Do not
open a public issue for a suspected vulnerability and do not include credentials, tenant
identifiers, internal addresses, customer data, or exploit details in public discussions.

Include the affected revision, deployment mode, prerequisites, impact, and a minimal
reproduction with synthetic data. Maintainers will acknowledge a valid report through the
private advisory and coordinate remediation and disclosure there.

## Supported versions

Only the latest release and the current `main` branch receive security fixes.

## Security boundaries

- Jira and FireFlow communication must use HTTPS.
- Secrets belong in the protected Linux or Docker paths documented in the deployment guide.
- Public releases may contain the self-contained bus and its ready Docker image. They must
  never contain runtime configuration, credentials, receipts, state, logs, or live screenshots.
- The setup wizard accepts an existing dedicated integration account and never grants ASMS
  or FireFlow roles. Administrators must validate the minimum rights for their deployment.
- The release image and installers are verified with the checksums published in the same
  GitHub Release. Runtime secrets are mounted or read from the protected host path.
