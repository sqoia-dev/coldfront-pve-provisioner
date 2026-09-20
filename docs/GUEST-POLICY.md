# Declarative guest policy

Declarative guest policy lets a site define the bounded software and non-secret
configuration that every managed VM should receive. It is disabled by default
and remains behind `PVE_PROVISIONER_EXECUTE`.

## Bootstrap the helper

Review `examples/coldfront_guest_reconcile.py.example`, install the reviewed
copy at the configured `guest_policy_helper` path, and make it root-owned and
non-writable by ordinary users. Bake it into the template when possible. A
protected cloud-init vendor snippet is also acceptable, but bootstrap must not
download an unpinned script at VM creation time.

The helper uses Python's standard library plus the selected guest package
manager and systemd. QGA must expose `guest-exec` and `guest-exec-status`. Those
RPCs are generic execution capabilities, so isolate the PVE credential even
though the plugin itself invokes only the configured helper with
`--apply-json`.

## Manifest contract

The plugin sends canonical JSON with schema `coldfront-pve-guest-policy/v1`:

```json
{
  "allocation": {
    "hostname": "coldfront-a51-v2000.example.org",
    "id": 51,
    "ipv4_address": "192.0.2.40",
    "users": ["alice", "bob"],
    "vmid": 2000
  },
  "files": [],
  "packages": {
    "manager": "dnf",
    "names": ["qemu-guest-agent", "sssd"],
    "patch_mode": "none"
  },
  "schema": "coldfront-pve-guest-policy/v1",
  "services": {
    "enable": true,
    "units": ["sssd.service"]
  }
}
```

The manifest has no command field. Package names cannot contain paths, shell
syntax, or options. Files are base64-encoded UTF-8 and bounded to 64 KiB each;
the whole manifest is bounded to 256 KiB. The reference helper rejects unsafe
roots, account/privilege files, symlinked path components, unknown fields, and
unsupported modes before applying work. Managed configuration files may not be
executable or world-writable.

Files are replaced atomically when content, mode, owner, or group differs.
Configured services are enabled and started; package or file changes also cause
`systemctl reload-or-restart`. When service enablement is disabled, package or
file changes use `systemctl try-reload-or-restart`, which leaves inactive units
inactive. Removing an admin inline does not delete the existing guest file.
Deliberate deletion remains an operator-owned migration.

## LDAP/SSSD allocation access example

Keep the site-wide SSSD domain, LDAPS URI, CA trust, NSS/PAM policy, and any bind
credential in the base image or approved secret delivery system. Use the admin
policy only for the allocation-specific, non-secret portion:

1. Select DNF and add the site-appropriate packages, for example `sssd` and
   `sssd-ldap`.
2. Add `sssd.service` as a systemd unit.
3. Add a managed file such as
   `/etc/sssd/conf.d/50-coldfront-allocation.conf`, owner/group `root`, mode
   `0600`.
4. Use content appropriate to the image's SSSD layout, for example:

   ```ini
   [domain/site]
   access_provider = ldap
   ldap_access_filter = {{ allocation_users_ldap_filter }}
   ```

The template renders exact validated ColdFront usernames:

```text
(|(uid=alice)(uid=bob))
```

With no active allocation users it renders a deliberately non-matching filter,
`(uid=__coldfront_no_selected_user__)`. Adding, removing, activating, or
deactivating an allocation user queues a durable reconciliation when
`guest_reconcile_on_membership_change` is enabled. The helper replaces the
fragment and reloads or restarts SSSD.

This mechanism controls only the rendered filter. Validate the complete SSSD
configuration and the consequences of cached credentials in a canary VM before
production use.

## Patching

`security` uses DNF security updates. `all` uses the selected package manager's
ordinary update/upgrade operation. APT security-only behavior is intentionally
rejected because it is not portable across supported Debian/Ubuntu layouts.

Set an interval to make `configure_pve_provisioner --apply` create a daily queue
schedule; each VM is queued only when its last successful patch is older than
the interval. With interval zero, operators can queue one VM explicitly:

```bash
coldfront patch_pve_vm <allocation-id> --dispatch
```

Patch jobs do not reboot guests, coordinate application drains, or perform
distribution upgrades. Handle maintenance windows and reboot detection through
site operations.

## SSH keys on the request page

The VM request page accepts an existing OpenSSH public key or can generate an
Ed25519 pair with Web Crypto. Generation and OpenSSH serialization happen in the
browser. The private key is downloaded as an unencrypted OpenSSH file, while
only its public line is placed in the form and later sent to cloud-init.

ColdFront cannot recover the private key. The requester must protect it with
local file mode `0600` and secure storage. Browsers without Web Crypto Ed25519
support retain the paste-existing-public-key path.
