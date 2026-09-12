from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .guest_access import render_access_reconcile_payload
from .models import (
    ProvisioningEvent,
    ProvisioningJob,
    VirtualMachine,
    get_configuration,
    get_flavor,
)
from .netbox import NetBoxClient
from .proxmox import ProxmoxClient
from .services import (
    desired_access_usernames,
    dispatch_job,
    request_ssh_public_key,
    safe_queue_access_reconciliation,
    sync_projections,
)


@transaction.atomic
def _claim_job(job_id):
    job = (
        ProvisioningJob.objects.select_for_update()
        .select_related("virtual_machine__allocation")
        .get(pk=job_id)
    )
    if job.status != ProvisioningJob.Status.QUEUED:
        return None
    job.status = ProvisioningJob.Status.RUNNING
    job.attempts += 1
    job.started_at = timezone.now()
    job.error = ""
    job.save(update_fields=["status", "attempts", "started_at", "error", "updated_at"])
    vm = job.virtual_machine
    retiring = job.action == ProvisioningJob.Action.RETIRE
    reconciling = job.action == ProvisioningJob.Action.RECONCILE
    if not reconciling:
        vm.state = (
            VirtualMachine.State.RETIRING
            if retiring
            else VirtualMachine.State.PROVISIONING
        )
    vm.last_error = ""
    vm.save(update_fields=["state", "last_error", "updated_at"])
    sync_projections(vm)
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        job=job,
        event_type=(
            "Retirement Worker Started"
            if retiring
            else "Directory Access Worker Started"
            if reconciling
            else "Worker Started"
        ),
    )
    return job.pk


def run_job(job_id):
    claimed = _claim_job(job_id)
    if not claimed:
        return {"status": "ignored", "job_id": str(job_id)}
    job = ProvisioningJob.objects.select_related("virtual_machine__allocation").get(
        pk=job_id
    )
    vm = job.virtual_machine
    if job.action == ProvisioningJob.Action.RETIRE:
        return _run_retirement_job(job, vm)
    if job.action == ProvisioningJob.Action.RECONCILE:
        return _run_access_reconciliation_job(job, vm)
    if not getattr(settings, "PVE_PROVISIONER_EXECUTE", False):
        error = "External provisioning gate PVE_PROVISIONER_EXECUTE is disabled."
        _finish(
            job.pk, ProvisioningJob.Status.BLOCKED, VirtualMachine.State.BLOCKED, error
        )
        return {"status": "blocked", "job_id": str(job_id)}
    try:
        configuration = get_configuration()
        flavor = get_flavor(vm.flavor)
        ssh_public_key = request_ssh_public_key(vm.allocation)
        netbox = NetBoxClient()
        netbox_ids = netbox.ensure_reservation(vm, flavor)
        _record_netbox_identity(vm.pk, netbox_ids)
        vm.refresh_from_db()
        pve = ProxmoxClient()
        target_node = pve.ensure_vm(
            vm,
            flavor,
            ssh_public_key,
            progress=lambda event_type, metadata=None: _record_progress(
                vm.pk, job.pk, event_type, metadata or {}
            ),
        )
        if configuration.guest_access_enabled:
            desired = desired_access_usernames(vm.allocation)
            pve.guest_exec(
                target_node,
                vm.vmid,
                [configuration.guest_access_helper],
                input_data=render_access_reconcile_payload(desired),
                timeout=600,
            )
            _record_access_sync(vm.pk, job.pk, desired)
        netbox.activate(vm)
        _record_progress(vm.pk, job.pk, "NetBox Activated", {})
    except Exception as exc:
        error = str(exc)[:4000]
        if _schedule_transient_retry(job.pk, error):
            return {"status": "retry_scheduled", "job_id": str(job_id)}
        _finish(
            job.pk, ProvisioningJob.Status.FAILED, VirtualMachine.State.FAILED, error
        )
        raise
    _finish(
        job.pk,
        ProvisioningJob.Status.SUCCEEDED,
        VirtualMachine.State.ACTIVE,
        "",
        target_node=target_node,
    )
    safe_queue_access_reconciliation(vm.allocation_id)
    return {"status": "succeeded", "job_id": str(job_id), "vmid": vm.vmid}


def _run_access_reconciliation_job(job, vm):
    configuration = get_configuration()
    if not configuration.guest_access_enabled:
        _finish_access(
            job.pk, ProvisioningJob.Status.BLOCKED, "Guest access sync is disabled."
        )
        return {"status": "blocked", "job_id": str(job.pk)}
    if not getattr(settings, "PVE_PROVISIONER_EXECUTE", False):
        error = "External provisioning gate PVE_PROVISIONER_EXECUTE is disabled."
        _finish_access(job.pk, ProvisioningJob.Status.BLOCKED, error)
        return {"status": "blocked", "job_id": str(job.pk)}
    desired = desired_access_usernames(vm.allocation)
    try:
        pve = ProxmoxClient()
        target = pve.require_exact_vm(vm)
        pve.guest_exec(
            target,
            vm.vmid,
            [configuration.guest_access_helper],
            input_data=render_access_reconcile_payload(desired),
            timeout=600,
        )
    except Exception as exc:
        error = str(exc)[:4000]
        _finish_access(job.pk, ProvisioningJob.Status.FAILED, error)
        raise
    _record_access_sync(vm.pk, job.pk, desired)
    _finish_access(job.pk, ProvisioningJob.Status.SUCCEEDED, "")
    safe_queue_access_reconciliation(vm.allocation_id)
    return {
        "status": "succeeded",
        "job_id": str(job.pk),
        "vmid": vm.vmid,
        "usernames": desired,
    }


def _run_retirement_job(job, vm):
    configuration = get_configuration()
    if not configuration.retirement_enabled:
        error = "Guarded retirement is disabled in Django admin."
        _finish(
            job.pk,
            ProvisioningJob.Status.BLOCKED,
            VirtualMachine.State.RETIREMENT_REVIEW,
            error,
        )
        return {"status": "blocked", "job_id": str(job.pk)}
    if vm.allocation.status.name == "Active":
        error = "The allocation is still Active; refusing VM retirement."
        _finish(
            job.pk,
            ProvisioningJob.Status.FAILED,
            VirtualMachine.State.RETIREMENT_REVIEW,
            error,
        )
        raise RuntimeError(error)
    if not getattr(settings, "PVE_PROVISIONER_EXECUTE", False) or not getattr(
        settings, "PVE_PROVISIONER_RETIRE", False
    ):
        error = "External VM retirement gates are disabled."
        _finish(
            job.pk,
            ProvisioningJob.Status.BLOCKED,
            VirtualMachine.State.RETIREMENT_REVIEW,
            error,
        )
        return {"status": "blocked", "job_id": str(job.pk)}
    try:
        pve = ProxmoxClient()
        if vm.pve_deleted_at is None:
            # Validate both external identities before the first destructive
            # action, then require a recoverable PBS snapshot.
            NetBoxClient().validate_exact_managed_records(vm)
            if not vm.retirement_backup_volume:
                backup = pve.find_exact_retirement_backup(vm)
                if backup is None:
                    if not vm.events.filter(
                        event_type="Retirement Backup Intent"
                    ).exists():
                        pve.require_exact_vm(vm)
                        ProvisioningEvent.objects.create(
                            virtual_machine=vm,
                            job=job,
                            event_type="Retirement Backup Intent",
                            metadata={
                                "vmid": vm.vmid,
                                "retention_days": configuration.retirement_backup_retention_days,
                            },
                        )
                    if not vm.retirement_backup_upid:
                        upid = pve.start_retirement_backup(vm)
                        _record_backup_upid(vm.pk, job.pk, upid)
                        vm.refresh_from_db()
                    pve.wait_task(
                        vm.target_node, vm.retirement_backup_upid, timeout=3600
                    )
                    backup = pve.find_exact_retirement_backup(vm)
                if backup is None:
                    raise RuntimeError(
                        "PBS completed without an exact ColdFront retirement backup."
                    )
                _record_retirement_backup(vm.pk, job.pk, backup)
                vm.refresh_from_db()
            pve.require_exact_retirement_backup(vm)
            pve_intent = vm.events.filter(
                event_type="Proxmox Retirement Intent"
            ).exists()
            if not pve_intent:
                pve.require_exact_vm(vm)
                ProvisioningEvent.objects.create(
                    virtual_machine=vm,
                    job=job,
                    event_type="Proxmox Retirement Intent",
                    metadata={
                        "vmid": vm.vmid,
                        "hostname": vm.hostname,
                        "target_node": vm.target_node,
                    },
                )
            pve.delete_exact_vm(vm, allow_absent=True)
            _record_retirement_phase(
                vm.pk, job.pk, "pve_deleted_at", "Proxmox VM Retired"
            )
            vm.refresh_from_db()

        netbox = NetBoxClient()
        if vm.netbox_deleted_at is None:
            netbox_intent = vm.events.filter(
                event_type="NetBox Retirement Intent"
            ).exists()
            if not netbox_intent:
                netbox.validate_exact_managed_records(vm)
                ProvisioningEvent.objects.create(
                    virtual_machine=vm,
                    job=job,
                    event_type="NetBox Retirement Intent",
                    metadata={
                        "vm_id": vm.netbox_vm_id,
                        "interface_id": vm.netbox_interface_id,
                        "ip_id": vm.netbox_ip_id,
                    },
                )
            netbox.delete_exact_managed_records(vm, allow_absent=True)
            _record_retirement_phase(
                vm.pk, job.pk, "netbox_deleted_at", "NetBox Records Retired"
            )
    except Exception as exc:
        error = str(exc)[:4000]
        _finish(
            job.pk,
            ProvisioningJob.Status.FAILED,
            VirtualMachine.State.RETIREMENT_REVIEW,
            error,
        )
        raise
    _finish(job.pk, ProvisioningJob.Status.SUCCEEDED, VirtualMachine.State.RETIRED, "")
    return {"status": "retired", "job_id": str(job.pk), "vmid": vm.vmid}


def cleanup_expired_retirement_backups():
    configuration = get_configuration()
    if not configuration.retirement_enabled:
        return {"status": "blocked", "deleted": 0}
    if not getattr(settings, "PVE_PROVISIONER_EXECUTE", False) or not getattr(
        settings, "PVE_PROVISIONER_RETIRE", False
    ):
        return {"status": "blocked", "deleted": 0}
    deleted = 0
    pve = ProxmoxClient()
    due = VirtualMachine.objects.filter(
        state=VirtualMachine.State.RETIRED,
        retirement_backup_volume__gt="",
        retirement_backup_expires_at__lte=timezone.now(),
        retirement_backup_deleted_at__isnull=True,
    ).order_by("retirement_backup_expires_at", "pk")
    for vm in due:
        cleanup_intent = vm.events.filter(
            event_type="Retirement Backup Cleanup Intent"
        ).exists()
        if not cleanup_intent:
            pve.require_exact_retirement_backup(vm)
            ProvisioningEvent.objects.create(
                virtual_machine=vm,
                event_type="Retirement Backup Cleanup Intent",
                metadata={"volume": vm.retirement_backup_volume},
            )
        pve.delete_exact_retirement_backup(vm, allow_absent=cleanup_intent)
        _record_retirement_backup_deleted(vm.pk)
        deleted += 1
    return {"status": "succeeded", "deleted": deleted}


@transaction.atomic
def _record_backup_upid(vm_id, job_id, upid):
    vm = VirtualMachine.objects.select_for_update().get(pk=vm_id)
    if vm.retirement_backup_upid and vm.retirement_backup_upid != upid:
        raise RuntimeError("A different retirement backup task is already recorded.")
    vm.retirement_backup_upid = upid
    vm.save(update_fields=["retirement_backup_upid", "updated_at"])
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        job_id=job_id,
        event_type="Retirement Backup Started",
        metadata={"upid": upid},
    )


@transaction.atomic
def _record_retirement_backup(vm_id, job_id, backup):
    configuration = get_configuration()
    vm = VirtualMachine.objects.select_for_update().get(pk=vm_id)
    volume = backup["volid"]
    created_at = datetime.fromtimestamp(int(backup["ctime"]), tz=datetime_timezone.utc)
    if vm.retirement_backup_volume and vm.retirement_backup_volume != volume:
        raise RuntimeError("A different retirement backup volume is already recorded.")
    vm.retirement_backup_volume = volume
    vm.retirement_backup_created_at = created_at
    vm.save(
        update_fields=[
            "retirement_backup_volume",
            "retirement_backup_created_at",
            "updated_at",
        ]
    )
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        job_id=job_id,
        event_type="Retirement Backup Ready",
        metadata={
            "volume": volume,
            "retention_days_after_retirement": configuration.retirement_backup_retention_days,
        },
    )


@transaction.atomic
def _record_retirement_backup_deleted(vm_id):
    vm = VirtualMachine.objects.select_for_update().get(pk=vm_id)
    if vm.retirement_backup_deleted_at is None:
        vm.retirement_backup_deleted_at = timezone.now()
        vm.save(update_fields=["retirement_backup_deleted_at", "updated_at"])
        ProvisioningEvent.objects.create(
            virtual_machine=vm,
            event_type="Retirement Backup Deleted",
            metadata={"volume": vm.retirement_backup_volume},
        )


@transaction.atomic
def _record_retirement_phase(vm_id, job_id, field, event_type):
    if field not in {"pve_deleted_at", "netbox_deleted_at"}:
        raise ValueError("Unknown retirement phase field.")
    vm = VirtualMachine.objects.select_for_update().get(pk=vm_id)
    if getattr(vm, field) is None:
        setattr(vm, field, timezone.now())
        vm.save(update_fields=[field, "updated_at"])
        ProvisioningEvent.objects.create(
            virtual_machine=vm,
            job_id=job_id,
            event_type=event_type,
        )


@transaction.atomic
def _record_netbox_identity(vm_id, ids):
    vm = VirtualMachine.objects.select_for_update().get(pk=vm_id)
    expected = {
        "netbox_vm_id": ids["vm_id"],
        "netbox_interface_id": ids["interface_id"],
        "netbox_ip_id": ids["ip_id"],
    }
    for field, value in expected.items():
        current = getattr(vm, field)
        if current not in (None, value):
            raise RuntimeError(
                f"Stored {field} disagrees with NetBox; refusing identity replacement."
            )
        setattr(vm, field, value)
    vm.save(update_fields=[*expected, "updated_at"])
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        event_type="NetBox Reservation Confirmed",
        metadata=ids,
    )
    sync_projections(vm)


def _record_progress(vm_id, job_id, event_type, metadata):
    """Persist an externally completed provisioning milestone for live UI polling."""
    ProvisioningEvent.objects.create(
        virtual_machine_id=vm_id,
        job_id=job_id,
        event_type=event_type,
        metadata=metadata,
    )


def _is_transient_provisioning_error(error):
    detail = error.lower()
    permanent = (
        "refusing collision",
        "differs from",
        "permission denied",
        "http 400",
        "http 401",
        "http 403",
        "not configured",
        "not safe",
    )
    if any(marker in detail for marker in permanent):
        return False
    transient = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection refused",
        "connection reset",
        "connection aborted",
        "name or service not known",
        "could not resolve",
        "guest agent is not running",
        "transient directory lookup failed",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    )
    return any(marker in detail for marker in transient)


@transaction.atomic
def _schedule_transient_retry(job_id, error):
    job = (
        ProvisioningJob.objects.select_for_update()
        .select_related("virtual_machine")
        .get(pk=job_id)
    )
    if job.action != ProvisioningJob.Action.PROVISION or job.attempts >= 3:
        return False
    if not _is_transient_provisioning_error(error):
        return False
    job.status = ProvisioningJob.Status.QUEUED
    job.error = error
    job.completed_at = None
    job.save(update_fields=["status", "error", "completed_at", "updated_at"])
    vm = job.virtual_machine
    vm.state = VirtualMachine.State.QUEUED
    vm.last_error = error
    vm.save(update_fields=["state", "last_error", "updated_at"])
    sync_projections(vm)
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        job=job,
        event_type="Automatic Retry Scheduled",
        metadata={"attempt": job.attempts, "delay_seconds": 300},
    )
    return True


@transaction.atomic
def _record_access_sync(vm_id, job_id, usernames):
    vm = VirtualMachine.objects.select_for_update().get(pk=vm_id)
    vm.access_desired_users = usernames
    vm.access_applied_users = usernames
    vm.access_last_error = ""
    vm.access_synced_at = timezone.now()
    vm.save(
        update_fields=[
            "access_desired_users",
            "access_applied_users",
            "access_last_error",
            "access_synced_at",
            "updated_at",
        ]
    )
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        job_id=job_id,
        event_type="Directory Access Synchronized",
        metadata={"usernames": usernames},
    )


@transaction.atomic
def _finish_access(job_id, job_status, error):
    job = (
        ProvisioningJob.objects.select_for_update()
        .select_related("virtual_machine")
        .get(pk=job_id)
    )
    job.status = job_status
    job.error = error
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "error", "completed_at", "updated_at"])
    vm = job.virtual_machine
    vm.access_last_error = error
    vm.last_error = error
    vm.state = (
        VirtualMachine.State.ACTIVE
        if job_status == ProvisioningJob.Status.SUCCEEDED
        else VirtualMachine.State.FAILED
    )
    vm.save(update_fields=["access_last_error", "last_error", "state", "updated_at"])
    sync_projections(vm)
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        job=job,
        event_type=f"Directory Access {job_status}",
        metadata={"error": error},
    )


@transaction.atomic
def _finish(job_id, job_status, vm_state, error, target_node=""):
    job = (
        ProvisioningJob.objects.select_for_update()
        .select_related("virtual_machine")
        .get(pk=job_id)
    )
    job.status = job_status
    job.error = error
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "error", "completed_at", "updated_at"])
    vm = job.virtual_machine
    vm.state = vm_state
    vm.last_error = error
    if target_node:
        vm.target_node = target_node
    if vm_state == VirtualMachine.State.ACTIVE:
        vm.provisioned_at = timezone.now()
    if vm_state == VirtualMachine.State.RETIRED:
        vm.retired_at = timezone.now()
        vm.retirement_backup_expires_at = vm.retired_at + timedelta(
            days=get_configuration().retirement_backup_retention_days
        )
    vm.save(
        update_fields=[
            "state",
            "last_error",
            "target_node",
            "provisioned_at",
            "retired_at",
            "retirement_backup_expires_at",
            "updated_at",
        ]
    )
    sync_projections(vm)
    ProvisioningEvent.objects.create(
        virtual_machine=vm,
        job=job,
        event_type=f"Worker {job_status}",
        metadata={"target_node": target_node, "error": error},
    )


def dispatch_queued_jobs():
    # Never reclaim a legitimate PBS-backed retirement while its allowed
    # Django-Q worker window is still open.
    retry_seconds = int(settings.Q_CLUSTER.get("retry", 3660))
    stale_before = timezone.now() - timedelta(seconds=retry_seconds + 60)
    ProvisioningJob.objects.filter(
        status=ProvisioningJob.Status.RUNNING,
        started_at__lt=stale_before,
        attempts__lt=3,
    ).update(status=ProvisioningJob.Status.QUEUED, error="Recovered stale worker claim")
    dispatched = 0
    retry_before = timezone.now() - timedelta(seconds=300)
    for job in (
        ProvisioningJob.objects.select_related("virtual_machine")
        .filter(
            status=ProvisioningJob.Status.QUEUED,
            attempts__lt=3,
        )
        .filter(Q(attempts=0) | Q(updated_at__lte=retry_before))
        .order_by("queued_at")[:10]
    ):
        dispatch_job(job)
        dispatched += 1
    return {"dispatched": dispatched}
