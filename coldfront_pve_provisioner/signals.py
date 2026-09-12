from functools import partial

from coldfront.core.allocation.models import Allocation, AllocationUser
from coldfront.core.allocation.signals import allocation_activate, allocation_disable
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .services import (
    safe_queue_access_reconciliation,
    safe_queue_allocation,
    safe_queue_retirement,
)


@receiver(
    allocation_activate,
    dispatch_uid="coldfront.pve.provisioner.queue_approved_vm",
)
def queue_approved_vm(sender, allocation_pk, **kwargs):
    transaction.on_commit(partial(safe_queue_allocation, allocation_pk), robust=True)


@receiver(
    allocation_disable,
    dispatch_uid="coldfront.pve.provisioner.require_retirement_review",
)
def flag_disabled_vm(sender, allocation_pk, **kwargs):
    transaction.on_commit(partial(safe_queue_retirement, allocation_pk), robust=True)


@receiver(
    post_save,
    sender=Allocation,
    dispatch_uid="coldfront.pve.provisioner.queue_expired_vm_retirement",
)
def queue_expired_vm_retirement(sender, instance, **kwargs):
    if instance.status.name == "Expired" and hasattr(instance, "pve_provisioned_vm"):
        transaction.on_commit(partial(safe_queue_retirement, instance.pk), robust=True)


def _queue_access_change(instance):
    transaction.on_commit(
        partial(safe_queue_access_reconciliation, instance.allocation_id), robust=True
    )


@receiver(
    post_save,
    sender=AllocationUser,
    dispatch_uid="coldfront.pve.provisioner.reconcile_added_vm_user",
)
def reconcile_added_vm_user(sender, instance, **kwargs):
    _queue_access_change(instance)


@receiver(
    post_delete,
    sender=AllocationUser,
    dispatch_uid="coldfront.pve.provisioner.reconcile_removed_vm_user",
)
def reconcile_removed_vm_user(sender, instance, **kwargs):
    _queue_access_change(instance)
