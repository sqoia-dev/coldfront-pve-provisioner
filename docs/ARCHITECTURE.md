# Architecture and lifecycle

## Authority split

- **ColdFront**: request, approval, allocation users, authoritative VM/IP
  reservation, durable job state, and audit events.
- **NetBox (optional)**: mirrored VM/interface/IP inventory and lease status.
- **Proxmox VE**: VM runtime, template, task, and backup state.
- **Django admin**: non-secret provisioning policy.
- **Guest helper**: independently validates and applies one versioned,
  declarative manifest inside the VM.
- **Protected settings**: credentials, TLS trust, and mutation kill switches.

Signals only register transaction-on-commit callbacks. The callback creates an
idempotent database job and dispatches it to Django-Q; no infrastructure call is
made in the request transaction.

## Identity invariant

One live or recoverable ColdFront VM owns one VMID, IPv4 address, hostname, and
template identity. When NetBox mirroring is enabled it also owns one exact
NetBox record set. A retired identity remains reserved until its recovery backup
is deleted. Mirrored external records must contain the managed tag and exact
allocation-bound description before mutation proceeds.

## Provisioning state machine

`Reserved → Queued → Provisioning → Starting → Active`

Failures become `Blocked` when authority is disabled and `Failed` for exhausted
or non-transient errors. Network, DNS, selected HTTP 5xx, and directory outages
are retried with the same identity. Collision, permission, and identity mismatch
errors are not retried as if they were outages.

## Retirement state machine

`Active → Retirement Review → Retiring → Retired`

The worker validates PVE and, when configured, NetBox before the first
destructive action, creates/records/verifies a PBS snapshot, records intent,
deletes PVE, then deletes any mirrored NetBox records. Each phase is resumable.
Cleanup deletes only the exact recorded backup after retention and releases
identity reuse only after that deletion.

## Guest policy and reconciliation

The plugin renders `coldfront-pve-guest-policy/v1` from typed admin policy plus
the current allocation identity and active users. It invokes only the configured
helper path with `--apply-json`; the manifest cannot carry a command or package
manager option. The reference helper validates the schema again, uses fixed
DNF/APT/systemd argument vectors, and atomically replaces approved files.

Provisioning applies the current policy before the VM becomes active. Later
policy drift or allocation membership changes create durable `Reconcile` jobs.
Optional scheduled or manual `Patch` jobs use the same helper and record both
the steady policy hash and the exact patch-manifest hash. Guest-maintenance
failures remain visible without misrepresenting a running VM as absent.

LDAP transport, CA trust, PAM, sudo, home-directory behavior, bind credentials,
and institution-wide SSSD defaults remain site policy. The generic layer can
install named packages and render a non-secret, allocation-specific access
fragment, including a fail-closed LDAP UID filter.

The QEMU Guest Agent exposes generic `guest-exec`; it does not normally enforce
a per-executable allowlist. Protect and scope the PVE token accordingly. This
plugin only requests the configured helper paths, but that application behavior
is not a substitute for credential isolation.

## SSH request boundary

The optional frontend generator uses Web Crypto in the requester's browser. It
downloads an OpenSSH Ed25519 private key locally and fills the form with only
the public key. Django validates and retains that public key; PVE passes it to
cloud-init. Neither the guest helper nor the server receives the private key.
