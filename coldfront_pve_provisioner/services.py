import logging

from coldfront.core.allocation.models import (
    Allocation,
    AllocationAttribute,
    AllocationAttributeType,
)
from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.utils import timezone

from .constants import hostname_for, ipv4_for_vmid
from .guest_access import normalize_usernames
from .guest_policy import render_guest_policy
from .models import (
    ProvisioningEvent,
    ProvisioningJob,
    VirtualMachine,
    VMIdentityPool,
    VMRequest,
    get_configuration,
)
from .validators import validate_flavor, validate_ssh_public_key

logger = logging.getLogger(__name__)


def is_vm_allocation(allocation):
    return allocation.resources.filter(name=get_configuration().resource_name).exists()


def validate_initial_service_term(allocation):
    configuration = get_configuration()
    if not allocation.start_date or not allocation.end_date:
        raise RuntimeError(
            "The Proxmox VM allocation has no policy-owned service term."
        )
    expected_end = allocation.start_date + relativedelta(
        months=configuration.service_term_months
    )
    if allocation.end_date != expected_end:
        raise RuntimeError(
            f"The initial PVE VM service term must be exactly {configuration.service_term_months} "
            "calendar months; "
            "refusing provisioning."
        )


def allocation_attribute(allocation, name):
    row = allocation.allocationattribute_set.filter(
        allocation_attribute_type__name=name
    ).first()
    return row.value if row else ""


def request_ssh_public_key(allocation):
    request = (
        VMRequest.objects.filter(allocation=allocation).only("ssh_public_key").first()
    )
    if not request:
        raise RuntimeError(
            "The VM request has no retained SSH public key; refusing provisioning."
        )
    return validate_ssh_public_key(request.ssh_public_key)


def desired_access_usernames(allocation):
    return normalize_usernames(
        allocation.allocationuser_set.filter(
            status__name="Active", user__is_active=True
        )
        .order_by("user__username")
        .values_list("user__username", flat=True)
    )


def set_projection(allocation, name, value):
    # ColdFront AllocationAttribute.value is varchar(128). The plugin models
    # retain full operational errors and request material; these rows are only
    # short UI projections and must never make worker finalization fail.
    value = str(value)[:128]
    attribute_type = AllocationAttributeType.objects.get(name=name)
    AllocationAttribute.objects.update_or_create(
        allocation=allocation,
        allocation_attribute_type=attribute_type,
        defaults={"value": value},
    )


def sync_projections(vm):
    configuration = get_configuration()
    for name, value in (
        ("VM ID", vm.vmid),
        ("VM IPv4 Address", vm.ipv4_address),
        ("VM Hostname", vm.hostname),
        ("VM Proxmox Node", vm.target_node or "Not selected"),
        ("VM Template ID", vm.template_vmid),
        (
            "VM NetBox Record ID",
            vm.netbox_vm_id
            or ("Not created" if configuration.netbox_enabled else "Not enabled"),
        ),
        ("Provisioning State", vm.state),
        ("VM Provisioning Error", vm.last_error),
    ):
        set_projection(vm.allocation, name, value)


@transaction.atomic
def reserve_identity(allocation_id):
    configuration = get_configuration()
    allocation = (
        Allocation.objects.select_for_update()
        .select_related("status")
        .get(pk=allocation_id)
    )
    if allocation.status.name != "Active" or not is_vm_allocation(allocation):
        return None
    existing = VirtualMachine.objects.filter(allocation=allocation).first()
    if existing:
        if existing.state == VirtualMachine.State.RETIRED:
            raise RuntimeError(
                "A retired VM allocation cannot be reactivated; create a new allocation."
            )
        return existing
    validate_initial_service_term(allocation)
    flavor = validate_flavor(allocation_attribute(allocation, "VM Flavor"))
    request_ssh_public_key(allocation)
    VMIdentityPool.objects.select_for_update().get_or_create(singleton=1)
    # A retired identity remains reserved while its recovery backup is
    # retained. Reusing a VMID earlier would make recovery ambiguous.
    used = set(
        VirtualMachine.objects.filter(
            retirement_backup_deleted_at__isnull=True
        ).values_list("vmid", flat=True)
    )
    vmid = next(
        (
            candidate
            for candidate in range(configuration.vmid_min, configuration.vmid_max + 1)
            if candidate not in used
        ),
        None,
    )
    if vmid is None:
        raise RuntimeError("The ColdFront Proxmox VM identity pool is exhausted.")
    vm = VirtualMachine.objects.create(
        allocation=allocation,
        vmid=vmid,
        ipv4_address=ipv4_for_vmid(configuration, vmid),
        hostname=hostname_for(configuration, allocation.pk, vmid),
        flavor=flavor,
        template_vmid=configuration.template_vmid,
        state=VirtualMachine.State.RESERVED,
    )
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        event_type="Identity Reserved",
        metadata={
            "vmid": vm.vmid,
            "ipv4_address": vm.ipv4_address,
            "hostname": vm.hostname,
        },
    )
    sync_projections(vm)
    return vm


@transaction.atomic
def create_provisioning_job(allocation_id):
    vm = reserve_identity(allocation_id)
    if vm is None:
        return None
    if vm.state == VirtualMachine.State.ACTIVE:
        return None
    open_job = vm.provisioning_jobs.filter(
        status__in=(ProvisioningJob.Status.QUEUED, ProvisioningJob.Status.RUNNING)
    ).first()
    if open_job:
        return open_job
    job = ProvisioningJob.objects.create(virtual_machine=vm)
    vm.state = VirtualMachine.State.QUEUED
    vm.last_error = ""
    vm.save(update_fields=["state", "last_error", "updated_at"])
    sync_projections(vm)
    ProvisioningEvent.objects.create(
        virtual_machine=vm, job=job, event_type="Provisioning Queued"
    )
    return job


def dispatch_job(job):
    from django_q.tasks import async_task

    task_id = async_task(
        "coldfront_pve_provisioner.tasks.run_job",
        str(job.pk),
        task_name=f"coldfront-proxmox-{job.virtual_machine.vmid}-{job.pk}",
    )
    ProvisioningJob.objects.filter(pk=job.pk).update(django_q_task_id=str(task_id))


def safe_queue_allocation(allocation_id):
    try:
        job = create_provisioning_job(allocation_id)
        if job:
            dispatch_job(job)
    except Exception:
        logger.exception(
            "Unable to queue Proxmox VM allocation %s; approval remains recorded",
            allocation_id,
        )


@transaction.atomic
def create_access_reconciliation_job(allocation_id, *, membership_change=False):
    configuration = get_configuration()
    policy_requested = configuration.guest_policy_enabled and (
        not membership_change or configuration.guest_reconcile_on_membership_change
    )
    access_requested = configuration.guest_access_enabled
    if not policy_requested and not access_requested:
        return None
    vm = (
        VirtualMachine.objects.select_for_update()
        .select_related("allocation__status")
        .filter(allocation_id=allocation_id)
        .first()
    )
    if not vm or vm.allocation.status.name != "Active":
        return None
    if vm.state in (
        VirtualMachine.State.RESERVED,
        VirtualMachine.State.QUEUED,
        VirtualMachine.State.PROVISIONING,
        VirtualMachine.State.STARTING,
        VirtualMachine.State.RETIREMENT_REVIEW,
        VirtualMachine.State.RETIRING,
        VirtualMachine.State.RETIRED,
    ):
        return None
    desired = desired_access_usernames(vm.allocation)
    if vm.access_desired_users != desired:
        vm.access_desired_users = desired
        vm.save(update_fields=["access_desired_users", "updated_at"])
    access_needed = access_requested and (
        vm.access_applied_users != desired or bool(vm.access_last_error)
    )
    expected_policy_hash = ""
    if policy_requested:
        _, expected_policy_hash = render_guest_policy(
            configuration, vm, desired, apply_updates=False
        )
    policy_needed = policy_requested and (
        vm.guest_policy_hash != expected_policy_hash or bool(vm.guest_policy_last_error)
    )
    if not access_needed and not policy_needed:
        return None
    open_job = vm.provisioning_jobs.filter(
        action=ProvisioningJob.Action.RECONCILE,
        status__in=(ProvisioningJob.Status.QUEUED, ProvisioningJob.Status.RUNNING),
    ).first()
    if open_job:
        return open_job
    job = ProvisioningJob.objects.create(
        virtual_machine=vm,
        action=ProvisioningJob.Action.RECONCILE,
        metadata={
            "directory_access": access_needed,
            "guest_policy": policy_needed,
        },
    )
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        job=job,
        event_type="Guest Reconciliation Queued",
        metadata={
            "directory_access": access_needed,
            "guest_policy": policy_needed,
            "usernames": desired,
        },
    )
    return job


def safe_queue_access_reconciliation(allocation_id, *, membership_change=False):
    try:
        job = create_access_reconciliation_job(
            allocation_id, membership_change=membership_change
        )
        if job and not job.django_q_task_id:
            dispatch_job(job)
    except Exception:
        logger.exception(
            "Unable to queue VM guest reconciliation for allocation %s",
            allocation_id,
        )


@transaction.atomic
def create_guest_patch_job(allocation_id):
    configuration = get_configuration()
    if (
        not configuration.guest_policy_enabled
        or configuration.guest_patch_mode == configuration.GuestPatchMode.NONE
    ):
        return None
    vm = (
        VirtualMachine.objects.select_for_update()
        .select_related("allocation__status")
        .filter(allocation_id=allocation_id)
        .first()
    )
    if (
        not vm
        or vm.allocation.status.name != "Active"
        or vm.state != VirtualMachine.State.ACTIVE
    ):
        return None
    open_patch_job = vm.provisioning_jobs.filter(
        action=ProvisioningJob.Action.PATCH,
        status__in=(ProvisioningJob.Status.QUEUED, ProvisioningJob.Status.RUNNING),
    ).first()
    if open_patch_job:
        return open_patch_job
    reconciliation_running = vm.provisioning_jobs.filter(
        action=ProvisioningJob.Action.RECONCILE,
        status__in=(ProvisioningJob.Status.QUEUED, ProvisioningJob.Status.RUNNING),
    ).exists()
    if reconciliation_running:
        return None
    job = ProvisioningJob.objects.create(
        virtual_machine=vm,
        action=ProvisioningJob.Action.PATCH,
        metadata={"apply_updates": True, "guest_policy": True},
    )
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        job=job,
        event_type="Guest Patch Queued",
        metadata={"patch_mode": configuration.guest_patch_mode},
    )
    return job


@transaction.atomic
def create_retirement_job(allocation_id):
    if not get_configuration().retirement_enabled:
        return None
    vm = (
        VirtualMachine.objects.select_for_update()
        .select_related("allocation__status")
        .filter(allocation_id=allocation_id)
        .first()
    )
    if not vm:
        return None
    if vm.allocation.status.name == "Active":
        raise RuntimeError("The allocation is still Active; refusing VM retirement.")
    if vm.state == VirtualMachine.State.RETIRED:
        return None
    open_job = vm.provisioning_jobs.filter(
        status__in=(ProvisioningJob.Status.QUEUED, ProvisioningJob.Status.RUNNING)
    ).first()
    if open_job:
        return open_job
    job = ProvisioningJob.objects.create(
        virtual_machine=vm,
        action=ProvisioningJob.Action.RETIRE,
    )
    vm.state = VirtualMachine.State.RETIREMENT_REVIEW
    vm.last_error = ""
    vm.save(update_fields=["state", "last_error", "updated_at"])
    sync_projections(vm)
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        job=job,
        event_type="Retirement Queued",
        metadata={"allocation_status": vm.allocation.status.name},
    )
    return job


def safe_queue_retirement(allocation_id):
    try:
        require_retirement_review(allocation_id)
        job = create_retirement_job(allocation_id)
        if job and not job.django_q_task_id:
            dispatch_job(job)
    except Exception:
        logger.exception(
            "Unable to queue Proxmox VM retirement for allocation %s", allocation_id
        )


@transaction.atomic
def require_retirement_review(allocation_id):
    vm = (
        VirtualMachine.objects.select_for_update()
        .filter(allocation_id=allocation_id)
        .first()
    )
    if not vm or vm.state == VirtualMachine.State.RETIRED:
        return
    vm.state = VirtualMachine.State.RETIREMENT_REVIEW
    vm.save(update_fields=["state", "updated_at"])
    sync_projections(vm)
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        event_type="Retirement Review Required",
        metadata={
            "automatic_deletion": False,
            "recorded_at": timezone.now().isoformat(),
        },
    )
