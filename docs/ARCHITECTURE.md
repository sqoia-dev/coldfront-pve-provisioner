# Architecture and lifecycle

## Authority split

- **ColdFront**: request, approval, allocation users, authoritative VM/IP
  reservation, durable job state, and audit events.
- **NetBox (optional)**: mirrored VM/interface/IP inventory and lease status.
- **Proxmox VE**: VM runtime, template, task, and backup state.
- **Django admin**: non-secret provisioning policy.
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

## Guest access boundary

The optional adapter validates ColdFront usernames and sends only a sorted,
newline-delimited desired membership set to a configured in-guest helper. LDAP,
SSSD, PAM, sudo, home-directory, and institutional directory configuration are
site-owned image policy. The generic plugin does not generate those files.
