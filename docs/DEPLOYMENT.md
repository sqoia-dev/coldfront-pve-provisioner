# Deployment and rollback

## 1. Stage with mutation disabled

1. Back up the ColdFront database and local configuration.
2. Install an exact package version or commit in the ColdFront environment.
3. Add the app and URL configuration described in the README.
4. Set both execution gates to `False`.
5. Run migrations.
6. Create and validate the admin configuration and at least one flavor.
7. Run `coldfront configure_pve_provisioner --apply` and then `--check`.
8. Start/restart the Django-Q worker and verify its schedule appears healthy.

At this stage an approved allocation can create a blocked, auditable job but
cannot call PVE or an enabled NetBox mirror.

## 2. Verify external prerequisites read-only

- PVE and, when enabled, NetBox certificates chain to the configured trust store.
- The PVE token can read cluster resources, nodes, templates, VM configuration,
  task status, guest-agent status, and the configured storage.
- The template VMID and exact name match the admin configuration.
- Each allowed node is online and exposes the expected firewall-enabled bridge.
- The VMID range, IPv4 pool, and DNS zone are dedicated or explicitly
  coordinated. If NetBox is enabled, its cluster/tag are also dedicated.
- The configured cloud-init user and SSH policy exist in the template.
- If declarative guest policy is enabled, the exact reviewed helper is installed
  at `guest_policy_helper` in the image or protected bootstrap, owned by root,
  non-writable by ordinary users, and tested against a sample manifest.
- If the legacy guest-access adapter is enabled, its separate helper is tested.
- QGA reports `guest-exec` and `guest-exec-status`; operators understand these
  are generic execution RPCs and scope the PVE credential accordingly.
- If retirement is enabled, a disposable VM snapshot/restore exercise proves
  the configured PBS storage works before deletion is authorized.

## 3. Canary provisioning

Use a non-production ColdFront project, non-sensitive SSH key, dedicated test
range, and a maintenance window. Set only `PVE_PROVISIONER_EXECUTE=True`, restart
services, approve one allocation, and observe every durable event.

Verify ColdFront, the IP address reservations admin view, PVE, DNS/network
reachability, SSH identity, guest-agent readiness (when enabled), optional
NetBox records, declared packages/files/services, allocation-user removal, and
idempotent re-dispatch. If patching is enabled, test it separately and confirm
the guest's reboot requirements. Stop on any mismatch.

Download a browser-generated SSH key during the request canary, verify that only
its public line appears in the form/database, set local file mode `0600`, and
prove the private key logs in after provisioning. Also verify the paste-existing
key path for browsers that do not provide Web Crypto Ed25519.

## 4. Retirement canary

Retirement is a separate release. Confirm the canary has no needed data, enable
the admin retirement flag, set `PVE_PROVISIONER_RETIRE=True`, and retire only the
canary. Verify the PBS snapshot, exact intent records, PVE deletion, optional
NetBox deletion, retained identity reservation, and scheduled backup cleanup.

## Rollback

### Before external mutation

Set both execution gates to `False`, restart services, remove the URL override
and app only after queued jobs are resolved, then restore the database if the
migration itself must be reverted.

### After a VM was created

Do not delete it as an automatic software rollback. Disable execution, preserve
the ColdFront/PVE evidence plus any NetBox mirror, reconcile the exact identity manually, and
choose recovery or guarded retirement with the asset owner.

### After retirement started

Do not reuse the VMID or IP. Preserve the recorded PBS volume and intent events.
Restore from the exact backup only through a separately reviewed PVE recovery
procedure; this repository does not automate restore.
