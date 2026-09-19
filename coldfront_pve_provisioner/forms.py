from coldfront.core.allocation.forms import AllocationForm
from coldfront.core.allocation.models import Allocation
from coldfront.core.resource.models import Resource
from django import forms
from django.core.exceptions import ValidationError

from .models import ProvisionerFlavor, get_configuration, get_flavor
from .validators import validate_ssh_public_key

OPEN_ALLOCATION_STATUSES = (
    "Active",
    "New",
    "Renewal Requested",
    "Paid",
    "Payment Pending",
    "Payment Requested",
)


class PVEAllocationRequestForm(AllocationForm):
    vm_flavor = forms.ChoiceField(
        label="VM size",
        choices=(),
        required=False,
    )
    vm_ssh_public_key = forms.CharField(
        label="VM administrator SSH public key",
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 3, "spellcheck": "false", "autocomplete": "off"}
        ),
        help_text=(
            "Required for a VM request. Paste an existing public key or generate one "
            "in this browser. Only the public key is submitted and installed for the "
            "configured cloud user."
        ),
    )

    def __init__(self, request_user, project_pk, *args, **kwargs):
        super().__init__(request_user, project_pk, *args, **kwargs)
        configuration = get_configuration()
        self.fields["vm_flavor"].choices = [("", "Choose a VM size")] + [
            (flavor.code, flavor.label)
            for flavor in ProvisionerFlavor.objects.filter(enabled=True)
        ]
        vm_resource = Resource.objects.filter(
            name=configuration.resource_name,
            is_allocatable=True,
            is_available=True,
            is_public=True,
        ).first()
        resource_ids = list(
            self.fields["resource"].queryset.values_list("pk", flat=True)
        )
        if vm_resource:
            limit = vm_resource.get_attribute("allocation_limit", typed=True) or 1
            open_count = Allocation.objects.filter(
                project_id=project_pk,
                resources=vm_resource,
                status__name__in=OPEN_ALLOCATION_STATUSES,
            ).count()
            if open_count < limit:
                resource_ids.append(vm_resource.pk)
        self.fields["resource"].queryset = Resource.objects.filter(
            pk__in=resource_ids
        ).order_by("name")

    def clean(self):
        data = super().clean()
        resource = data.get("resource")
        configuration = get_configuration()
        if not resource or resource.name != configuration.resource_name:
            data["vm_flavor"] = ""
            data["vm_ssh_public_key"] = ""
            return data
        flavor = data.get("vm_flavor")
        try:
            get_flavor(flavor)
        except ValidationError:
            self.add_error("vm_flavor", "Choose an approved VM size.")
        try:
            data["vm_ssh_public_key"] = validate_ssh_public_key(
                data.get("vm_ssh_public_key")
            )
        except ValidationError as exc:
            self.add_error("vm_ssh_public_key", exc)
        data["quantity"] = 1
        self.instance.quantity = 1
        return data
