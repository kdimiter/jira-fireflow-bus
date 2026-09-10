# Network Access field

Native Forge UI Kit object field for Jira. One request contains 1–100 traffic lines;
each source/destination preserves one IP, CIDR, range or hostname value. Users can
add, duplicate and remove accesses and TCP/UDP services.

The committed manifest contains a placeholder app ID. Run `forge register` in a private
working copy before deployment; do not commit the registered ID or tenant details.

Run `npm ci`, `npm run check`, `npm test`, then `forge lint` and
`forge deploy --environment development`. Forge bundling needs TypeScript emission;
keep `--noEmit` in the check command, not tsconfig.

The form stores schemaVersion 1 directly in Jira. It has no hosted resolver,
Forge KVS, SQL or AI. The local bus consumes this object, not Description.
Credentials remain outside this repository. Installation and rendering are not proof of
a completed FireFlow workflow; validate the complete lifecycle in your own environment.
