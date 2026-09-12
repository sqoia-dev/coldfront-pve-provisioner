# Portability and known limits

## Generalized in 0.1

UC Merced network ranges, hostnames, PVE nodes/storage, template, guest username,
service term, flavors, NetBox identity, LDAP labels, and backup retention are no
longer compiled into the package. Site policy is typed and validated in Django
admin. The plugin depends only on public ColdFront interfaces, not a private
allocation UI package.

## Required assumptions

- One configuration profile and one ColdFront VM resource per deployment.
- Deterministic one-to-one VMID-to-IPv4 mapping.
- NetBox-backed inventory/IPAM.
- QEMU VMs cloned from one cloud-init template.
- One PVE bridge and storage policy across allowed target nodes.
- PVE firewall enabled on the inherited primary interface.
- Static IPv4 configuration; IPv6 and DHCP are not implemented.
- Optional user reconciliation through a site-provided QGA helper.
- Optional retirement requires PBS storage visible through PVE.

## Not yet portable

Alternative IPAM providers, multiple pools/templates/tenants, LXC, IPv6, DHCP,
DNS record mutation, quota/billing, HA placement constraints, online migration,
and automated restore are outside this release.

## UC Merced migration

The original private package used a different Django app label and site-specific
tables. This repository does not include an automatic production data migration.
An existing deployment must export and map its configuration and reservations in
a reviewed maintenance window; installing this package over it is not supported.
