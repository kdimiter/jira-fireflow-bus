# Contributing

Create a branch and submit a pull request to `main`. Keep each change focused and include
tests for behavior or security-boundary changes.

Before pushing, run:

```sh
python3 -m unittest discover -s tests
cd forge && npm ci && npm test && npm run check && npm audit --audit-level=high
```

Use synthetic tenant names, RFC 5737 example addresses, placeholder field IDs, and fictional
identities. Never commit API tokens, passwords, certificates, private keys, customer data,
internal hostnames, live request IDs, runtime container exports, logs, or screenshots
from a real environment.

Security reports belong in a private GitHub vulnerability report as described in
[SECURITY.md](SECURITY.md).
