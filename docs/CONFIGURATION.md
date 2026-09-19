# Configuration reference

PVE VM Provisioner separates site policy from secrets and execution authority.

## Django admin policy

Create exactly one **PVE provisioner configuration** row. The row defaults to
disabled and uses RFC 5737 documentation addresses; those defaults are not a
deployable network configuration.

### Catalog and term

| Field | Purpose |
| --- | --- |
| `enabled` | Allows resource recognition and job creation. It does not permit external mutation. |
| `resource_name` | Exact ColdFront resource name owned by the plugin. |
| `resource_description` | Public resource catalog description. |
| `allocation_limit` | Maximum open allocations for this resource per project. |
| `service_term_months` | Exact approved allocation term required before provisioning. |
| `operating_system` | Operator-facing image description. |
| `guest_username` | Cloud-init account that receives the submitted SSH key. |

### Built-in identity and IPv4 allocation

VMIDs map monotonically to IPv4 addresses: `ipv4_pool_start + (vmid - vmid_min)`.
The admin validator requires one usable address per VMID, one shared subnet for
the pool and gateway, and a gateway outside the allocation range. The built-in
database reservation is authoritative whether or not NetBox is enabled.

`hostname_template` accepts only `{allocation_id}`, `{vmid}`, and `{domain}`.
The default renders `coldfront-a1-v1100.example.org`.

Enter one resolver and one target PVE node per line in their respective text
fields. Whitespace-only lines are ignored.

The read-only **IP address reservations** admin view shows every current,
recovery-held, and released reservation. Released historical rows remain visible
even when a later VM safely reuses the address.

### PVE, optional NetBox, guest access, and retirement

The configured PVE template must be an actual QEMU template and its name must
match `template_name`. Every allowed node must expose the configured bridge with
PVE firewall enabled on the cloned interface.

NetBox inventory mirroring is disabled by default. When enabled, the plugin
creates or reuses one exact cluster type, cluster, and managed tag, then refuses
any record whose tag or description does not match the ColdFront allocation
identity. NetBox mirrors the built-in allocation; it does not choose addresses.

Guest access reconciliation is optional. When enabled, the image must contain
the configured helper and the QEMU Guest Agent must permit `guest-exec` and
`guest-exec-status`. The plugin sends newline-delimited, validated ColdFront
usernames to the helper; site-specific LDAP/SSSD policy belongs in the image and
helper, not this repository.

Guarded retirement is optional. Enabling it requires a PBS-backed PVE storage.
External deletion still requires `PVE_PROVISIONER_RETIRE=True`.

## Flavors

Create one or more **Provisioner flavors**. Codes are stable-style slugs and are
stored on reservations. Cores, memory MiB, and disk GiB determine PVE and
optional NetBox configuration. Disable an unused flavor instead of renaming its
code.

Once a flavor is referenced by a VM, its identity and dimensions are immutable.

## Protected Django settings

| Setting | Required | Description |
| --- | --- | --- |
| `PVE_PROVISIONER_API_URL` | yes | PVE API base ending in `/api2/json`. |
| `PVE_PROVISIONER_TOKEN_ID` | yes | Scoped PVE API token identifier. |
| `PVE_PROVISIONER_TOKEN_SECRET` | yes | Scoped PVE token secret. |
| `PVE_PROVISIONER_VERIFY_TLS` | yes | `True` or a CA bundle path. |
| `PVE_PROVISIONER_NETBOX_API_URL` | when NetBox is enabled | NetBox base URL. |
| `PVE_PROVISIONER_NETBOX_TOKEN` | when NetBox is enabled | Scoped NetBox API token. |
| `PVE_PROVISIONER_NETBOX_VERIFY_TLS` | when NetBox is enabled | `True` or a CA bundle path. |
| `PVE_PROVISIONER_EXECUTE` | yes | External provisioning/reconciliation kill switch; default behavior is false. |
| `PVE_PROVISIONER_RETIRE` | for retirement | Independent destructive-work kill switch. |

Restart ColdFront web and Django-Q services after protected setting changes.

## Configuration changes after use

The model rejects identity-affecting changes after any VM reservation exists.
This is intentional: existing audit records and external resources must retain
one meaning. Export the existing state and plan a migration rather than editing
around the guard.
