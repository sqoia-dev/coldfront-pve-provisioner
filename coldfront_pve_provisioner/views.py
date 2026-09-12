from coldfront.core.allocation.models import (
    Allocation,
    AllocationAttribute,
    AllocationAttributeType,
    AllocationPermission,
)
from coldfront.core.allocation.views import AllocationCreateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from .forms import PVEAllocationRequestForm
from .models import VirtualMachine, VMRequest, get_configuration

OPERATIONAL_STATES = {
    "Awaiting staff approval": ("Awaiting approval", "warning"),
    "Reserved": ("Queued", "warning"),
    "Queued": ("Queued", "warning"),
    "Provisioning": ("Provisioning", "info"),
    "Starting": ("Starting", "info"),
    "Active": ("Ready", "success"),
    "Blocked": ("Needs attention", "danger"),
    "Failed": ("Needs attention", "danger"),
    "Retirement Review": ("Retirement review", "warning"),
    "Retiring": ("Retiring", "warning"),
    "Retired": ("Retired", "secondary"),
}
TRANSITIONAL_STATES = {"Reserved", "Queued", "Provisioning", "Starting", "Retiring"}

PROVISIONING_MILESTONES = {
    "Identity Reserved": (5, "Identity reserved"),
    "Provisioning Queued": (10, "Queued for provisioning"),
    "Worker Started": (15, "Provisioning worker started"),
    "NetBox Reservation Confirmed": (25, "Network identity reserved"),
    "Proxmox VM Confirmed": (40, "VM clone confirmed"),
    "Proxmox Configuration Accepted": (55, "VM configuration accepted"),
    "Proxmox VM Running": (65, "VM running"),
    "SSH Ready": (75, "SSH reachable"),
    "Guest Agent Ready": (85, "Guest bootstrap ready"),
    "Directory Access Synchronized": (92, "Directory access synchronized"),
    "NetBox Activated": (97, "Network records activated"),
    "Worker Succeeded": (100, "Ready"),
}


def provisioning_progress(vm):
    if vm.state == VirtualMachine.State.ACTIVE:
        return None, ""
    if vm.state in (
        VirtualMachine.State.RETIREMENT_REVIEW,
        VirtualMachine.State.RETIRING,
        VirtualMachine.State.RETIRED,
    ):
        return None, ""
    event_types = []
    if hasattr(vm, "events"):
        event_types = vm.events.filter(
            event_type__in=PROVISIONING_MILESTONES
        ).values_list("event_type", flat=True)
    completed = [PROVISIONING_MILESTONES[event_type] for event_type in event_types]
    if completed:
        return max(completed, key=lambda item: item[0])
    fallback = {
        VirtualMachine.State.RESERVED: (5, "Identity reserved"),
        VirtualMachine.State.QUEUED: (10, "Queued for provisioning"),
        VirtualMachine.State.PROVISIONING: (15, "Provisioning worker started"),
        VirtualMachine.State.STARTING: (65, "VM starting"),
    }
    return fallback.get(vm.state, (0, "Awaiting provisioning"))


def operational_status_payload(state, allocation_status):
    label, badge_class = OPERATIONAL_STATES.get(
        state, (state or allocation_status, "secondary")
    )
    return {
        "state": state or allocation_status,
        "label": label,
        "badge_class": badge_class,
        "is_transitioning": state in TRANSITIONAL_STATES,
    }


def set_allocation_attribute(allocation, name, value):
    attribute_type = AllocationAttributeType.objects.get(name=name)
    attribute, _ = AllocationAttribute.objects.update_or_create(
        allocation=allocation,
        allocation_attribute_type=attribute_type,
        defaults={"value": str(value)},
    )
    return attribute


class PVEAllocationRequestView(AllocationCreateView):
    form_class = PVEAllocationRequestForm

    @transaction.atomic
    def form_valid(self, form):
        response = super().form_valid(form)
        resource = form.cleaned_data["resource"]
        configuration = get_configuration()
        if resource.name == configuration.resource_name:
            self.object.description = (
                f"{configuration.operating_system} PVE VM request; provisioning begins only "
                "after staff approval."
            )
            self.object.save(update_fields=["description", "modified"])
            VMRequest.objects.create(
                allocation=self.object,
                ssh_public_key=form.cleaned_data["vm_ssh_public_key"],
            )
            for name, value in (
                ("VM Flavor", form.cleaned_data["vm_flavor"]),
                ("Provisioning State", "Awaiting staff approval"),
            ):
                set_allocation_attribute(self.object, name, value)
        return response


class PVEAllocationStatusView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Return the authoritative VM state to viewers of the allocation."""

    def get_allocation(self):
        if not hasattr(self, "allocation"):
            self.allocation = get_object_or_404(
                Allocation.objects.select_related("status").filter(
                    resources__name=get_configuration().resource_name
                ),
                pk=self.kwargs["pk"],
            )
        return self.allocation

    def test_func(self):
        allocation = self.get_allocation()
        if self.request.user.has_perm("allocation.can_view_all_allocations"):
            return True
        return allocation.has_perm(self.request.user, AllocationPermission.USER)

    def get(self, request, *args, **kwargs):
        allocation = self.get_allocation()
        try:
            vm = allocation.pve_provisioned_vm
            state = vm.state
            progress_percent, progress_stage = provisioning_progress(vm)
        except VirtualMachine.DoesNotExist:
            vm = None
            state = (
                allocation.allocationattribute_set.filter(
                    allocation_attribute_type__name="Provisioning State"
                )
                .values_list("value", flat=True)
                .first()
                or allocation.status.name
            )
            progress_percent, progress_stage = (0, "Awaiting staff approval")
        payload = operational_status_payload(state, allocation.status.name)
        payload["progress_percent"] = progress_percent
        payload["progress_stage"] = progress_stage
        payload["allocation_status"] = allocation.status.name
        payload["ssh_username"] = get_configuration().guest_username
        response = JsonResponse(payload)
        response["Cache-Control"] = "private, no-store"
        return response
