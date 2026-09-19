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
- [ ] Django admin is limited to trusted infrastructure operators.
- [ ] Database and log backups protect allocation identity and audit history.
- [ ] Worker errors and retries are monitored.

## Guest access

- [ ] Guest access is disabled unless required.
- [ ] QGA permits only the minimum commands and the exact helper path.
- [ ] The helper validates stdin, replaces membership atomically, and fails closed.
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
