# PVE VM Provisioner for ColdFront

An administrator-operated ColdFront plugin that turns approved allocations into
guarded Proxmox VE virtual-machine workflows.

The provisioner reserves VM and network identities in ColdFront's database,
optionally mirrors them in NetBox, clones and configures a PVE cloud-init
template through a durable Django-Q worker, and records an append-only lifecycle
trail. Approval queues intent; it does not mutate infrastructure inside an HTTP
request or signal handler.

> [!WARNING]
> This project is alpha software. Its automated tests exercise policy,
> validation, API request construction, collision refusal, and retirement
> guards with mocks. It has not been validated against every ColdFront, PVE,
> NetBox, guest image, or production topology. Start with execution disabled.

## What is configurable

Non-secret site policy is stored in typed Django models and edited through the
Django admin:

- ColdFront resource name, description, allocation limit, and service term;
- enabled VM flavors;
- VMID and IPv4 pools;
- subnet, gateway, DNS, and hostname template;
- PVE template identity, target nodes, storage, bridge, CPU type, and pool;
- optional NetBox inventory mirroring, cluster identity, and managed tag;
- optional guest packages, managed files, systemd units, allocation-membership
  reconciliation, and patch cadence;
- optional legacy directory-access reconciliation; and
- optional guarded retirement, PBS storage, and backup retention.

Credentials and mutation gates stay in protected Django settings. They are not
stored in the database or exposed in Django admin.

Identity-affecting configuration and used flavor dimensions are frozen after the
first VM reservation. This prevents an admin edit from silently changing the
meaning of an existing VMID, IP, hostname, template, or backup.

## Supported stack

The initial public scope is deliberately narrow:

- ColdFront `>=1.1,<1.2`;
- Python 3.10 or newer;
- Django-Q2 through ColdFront;
- Proxmox VE QEMU virtual machines with cloud-init and a running QEMU Guest Agent;
- Linux guests with systemd and, when declarative guest policy is enabled, DNF
  or APT plus the versioned in-guest helper contract;
- built-in database-backed IPv4 allocation, with an optional NetBox inventory
  mirror; and
- optional Proxmox Backup Server storage for guarded retirement.

The built-in pool is authoritative and is visible through the read-only **IP
address reservations** admin view. Multiple independent provisioning profiles
are not yet implemented.

## Safety model

Three independent controls apply:

1. `ProvisionerConfiguration.enabled` allows this plugin to recognize and queue
   its configured resource.
2. `PVE_PROVISIONER_EXECUTE=True` permits external PVE mutation and, when
   enabled in admin, NetBox mirroring.
3. `PVE_PROVISIONER_RETIRE=True`, the admin `retirement_enabled` flag, and a
   configured PBS storage are all required before destructive retirement.

Without the relevant gate, work remains blocked and auditable. Provisioning
failure retains identity reservations. It never destroys a partially created VM
or releases an IP automatically.

Retirement validates exact ColdFront and PVE identity plus NetBox identity when
the optional mirror is enabled, creates and verifies a scoped PBS snapshot,
records intent before deletion, and keeps the VMID/IP reserved until the backup
expires and is removed.

Declarative guest policy accepts bounded data, not administrator-provided shell
commands: validated package names, files beneath approved roots, systemd units,
and a fixed patch mode. The reference helper independently validates the
versioned manifest before changing the guest.

The allocation form can generate an Ed25519 key pair in the requester's browser.
The private key is downloaded locally and is never submitted; only its public
half follows the existing cloud-init provisioning path.

## Installation

Install into the same environment as ColdFront:

```bash
python -m pip install coldfront-pve-provisioner
```

Until a package release exists, install an exact reviewed Git commit:

```bash
python -m pip install \
  "git+https://github.com/sqoia-dev/coldfront-pve-provisioner.git@<commit>"
```

Add the app and URL override in ColdFront's local configuration:

```python
INSTALLED_APPS.append("coldfront_pve_provisioner.apps.PVEProvisionerConfig")

# Put this before ColdFront's stock allocation-create route.
urlpatterns = [
    path("", include("coldfront_pve_provisioner.urls")),
    *urlpatterns,
]
```

Apply migrations, create the admin policy and at least one flavor, then synchronize
the ColdFront resource catalog:

```bash
coldfront migrate
coldfront createsuperuser
# In Django admin: create one PVE provisioner configuration and one enabled flavor.
coldfront configure_pve_provisioner --apply
coldfront configure_pve_provisioner --check
```

If declarative guest policy is enabled, install the reviewed reference helper
from `examples/coldfront_guest_reconcile.py.example` at the configured path in
the image or through a protected cloud-init bootstrap. See
[Guest policy](docs/GUEST-POLICY.md) before enabling it.

See [Deployment](docs/DEPLOYMENT.md) for the full sequence and rollback plan.

## Protected settings

Define these values in `/etc/coldfront/local_settings.py` or another protected
ColdFront settings source:

```python
PVE_PROVISIONER_API_URL = "https://pve.example.org:8006/api2/json"
PVE_PROVISIONER_TOKEN_ID = "coldfront@pve!provisioner"
PVE_PROVISIONER_TOKEN_SECRET = "read-from-your-secret-manager"
PVE_PROVISIONER_VERIFY_TLS = "/etc/pki/ca-trust/source/anchors/pve-ca.pem"

# Required only when "NetBox inventory mirroring" is enabled in Django admin.
# PVE_PROVISIONER_NETBOX_API_URL = "https://netbox.example.org"
# PVE_PROVISIONER_NETBOX_TOKEN = "read-from-your-secret-manager"
# PVE_PROVISIONER_NETBOX_VERIFY_TLS = True

PVE_PROVISIONER_EXECUTE = False
PVE_PROVISIONER_RETIRE = False
```

Do not commit secrets. Do not disable TLS verification for either configured
service in a real deployment; use the site CA bundle.

## Worker lifecycle

Provisioning is idempotent around durable milestones:

1. reserve VMID, IPv4 address, hostname, flavor, and template identity;
2. confirm the built-in IP reservation and, when enabled, reserve exact NetBox
   VM, interface, and IP records;
3. refuse PVE collisions or clone the configured template;
4. configure CPU, memory, cloud-init identity, SSH key, and network;
5. start the VM and wait for SSH;
6. optionally wait for the guest agent and reconcile declared packages, files,
   systemd units, and allocation-aware access policy;
7. optionally run the legacy directory-access helper;
8. activate optional NetBox records; and
9. mark the ColdFront VM active.

Transient network, DNS, HTTP 5xx, and directory outages reuse the same identity
and are retried. Permission errors, collisions, and identity drift fail closed.

## Verification

```bash
python -m compileall -q coldfront_pve_provisioner
DJANGO_SETTINGS_MODULE=test_settings python -m django test coldfront_pve_provisioner
DJANGO_SETTINGS_MODULE=test_settings python -m django makemigrations --check --dry-run
python -m build
```

The tests do not contact PVE, NetBox, PBS, DNS, LDAP, or a guest VM.

## Documentation

- [Configuration reference](docs/CONFIGURATION.md)
- [Deployment and rollback](docs/DEPLOYMENT.md)
- [Architecture and lifecycle](docs/ARCHITECTURE.md)
- [Declarative guest policy and LDAP/SSSD example](docs/GUEST-POLICY.md)
- [Security checklist](docs/SECURITY-CHECKLIST.md)
- [Portability and known limits](PORTABILITY.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License

Copyright © 2026 Robert Romero. Released under the GNU Affero General Public
License v3.0 or later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Maintained by Sqoia Labs. Sqoia Labs is an operating name; no separate legal
entity status is claimed by this repository.
