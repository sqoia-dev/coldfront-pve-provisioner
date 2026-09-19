"""Stable names and helpers for site-configurable provisioning policy."""

from ipaddress import ip_address

RESOURCE_TYPE_NAME = "Virtual Machine"
SCHEDULE_NAME = "PVE VM Provisioner queued-job dispatcher"
GUEST_PATCH_SCHEDULE_NAME = "PVE VM Provisioner guest patch queue"


def ipv4_for_vmid(configuration, vmid):
    vmid = int(vmid)
    if not configuration.vmid_min <= vmid <= configuration.vmid_max:
        raise ValueError(
            f"VMID must be in {configuration.vmid_min}-{configuration.vmid_max}."
        )
    address = ip_address(configuration.ipv4_pool_start) + (
        vmid - configuration.vmid_min
    )
    if address > ip_address(configuration.ipv4_pool_end):
        raise ValueError("The configured IPv4 pool is smaller than the VMID pool.")
    return str(address)


def hostname_for(configuration, allocation_id, vmid):
    allocation_id = int(allocation_id)
    if allocation_id < 1:
        raise ValueError("Allocation ID must be positive.")
    ipv4_for_vmid(configuration, vmid)
    return configuration.hostname_template.format(
        allocation_id=allocation_id,
        vmid=int(vmid),
        domain=configuration.dns_search_domain,
    ).rstrip(".")


def retirement_backup_notes(configuration, allocation_id):
    allocation_id = int(allocation_id)
    if allocation_id < 1:
        raise ValueError("Allocation ID must be positive.")
    return (
        f"ColdFront allocation {allocation_id} retirement recovery; retain "
        f"{configuration.retirement_backup_retention_days} days"
    )
