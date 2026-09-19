# Portability and known limits

## Generalized in 0.1

Institutional network ranges, hostnames, PVE nodes/storage, template, guest
username, service term, flavors, NetBox identity, LDAP policy, guest packages,
managed files, service units, patch cadence, and backup retention are not
compiled into the package. Site policy is typed and validated in Django admin.
The plugin depends only on public ColdFront interfaces, not a private allocation
UI package.

## Required assumptions

- One configuration profile and one ColdFront VM resource per deployment.
- Deterministic one-to-one VMID-to-IPv4 mapping.
- Built-in deterministic IPv4 allocation with optional NetBox inventory mirroring.
- QEMU VMs cloned from one cloud-init template.
- One PVE bridge and storage policy across allowed target nodes.
- PVE firewall enabled on the inherited primary interface.
- Static IPv4 configuration; IPv6 and DHCP are not implemented.
- Optional declarative reconciliation through the versioned reference helper on
  a systemd-based Linux guest using DNF or APT.
- Optional legacy user reconciliation through a separate site-provided helper.
- Optional retirement requires PBS storage visible through PVE.

## Not yet portable

External IPAM authority, multiple pools/templates/tenants, LXC, IPv6, DHCP,
DNS record mutation, quota/billing, HA placement constraints, online migration,
Windows guests, non-systemd service managers, package repository configuration,
secret delivery, automatic reboot orchestration, distribution upgrades, and
automated restore are outside this release.

## UC Merced migration

The original private package used a different Django app label and site-specific
tables. This repository does not include an automatic production data migration.
An existing deployment must export and map its configuration and reservations in
a reviewed maintenance window; installing this package over it is not supported.
