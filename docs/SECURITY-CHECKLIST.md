# Security checklist

## Before enabling execution

- [ ] PVE and any enabled NetBox mirror use valid TLS with verification enabled.
- [ ] Tokens are stored outside Git and the database.
- [ ] PVE token permissions are restricted to configured nodes, pool, storage,
      template, VMID range, and required task/guest-agent operations.
- [ ] When NetBox is enabled, its token permissions are restricted to the intended cluster, VM,
      interface, IP, and tag objects.
- [ ] The VMID/IP ranges and any configured managed tag cannot collide with another controller.
- [ ] The cloud image disables password and root SSH login.
- [ ] Submitted SSH keys are treated as sensitive operational metadata.
- [ ] Users know the browser-generated private key is unencrypted, downloaded
      once, and never recoverable from ColdFront.
- [ ] Django admin is limited to trusted infrastructure operators.
- [ ] Database and log backups protect allocation identity and audit history.
- [ ] Worker errors and retries are monitored.

## Guest policy and access

- [ ] Guest policy and legacy guest access are disabled unless required.
- [ ] The helper is installed as a root-owned reviewed artifact and ordinary
      guest users cannot replace it.
- [ ] The helper validates the exact manifest schema, file roots, package names,
      service units, size limits, and patch modes before mutation.
- [ ] Admin-managed files contain no passwords, tokens, private keys, or LDAP
      bind secrets.
- [ ] Operators account for the generic authority of QGA `guest-exec` when
      granting and storing PVE credentials.
- [ ] Package repositories and signing keys are trusted; patch behavior has a
      maintenance/reboot procedure outside this plugin.
- [ ] LDAP/SSSD CA validation, authorization, sudo denial, and cache policy are
      reviewed as site configuration.

## Retirement

- [ ] Retirement is disabled by default and independently authorized.
- [ ] A disposable restore exercise has passed on the configured PBS storage.
- [ ] Snapshot retention satisfies local policy.
- [ ] Operators understand that cleanup permanently removes the recovery copy.
- [ ] VMIDs and IPs are not manually reused while recovery backups exist.

## Incident response

Set `PVE_PROVISIONER_EXECUTE=False` and `PVE_PROVISIONER_RETIRE=False`, restart
workers, preserve database/events and external object state, and revoke affected
tokens. Do not “clean up” mismatched resources before evidence and ownership are
established.
