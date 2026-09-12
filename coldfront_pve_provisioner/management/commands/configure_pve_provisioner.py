from coldfront.core.allocation.models import AllocationAttributeType
from coldfront.core.allocation.models import AttributeType as AllocationValueType
from coldfront.core.resource.models import (
    AttributeType,
    Resource,
    ResourceAttribute,
    ResourceAttributeType,
    ResourceType,
)
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_q.models import Schedule

from ...constants import RESOURCE_TYPE_NAME, SCHEDULE_NAME
from ...models import ProvisionerFlavor, VMIdentityPool, get_configuration

ALLOCATION_ATTRIBUTES = {
    "VM Flavor": ("Text", False, False),
    "VM ID": ("Int", False, False),
    "VM IPv4 Address": ("Text", False, False),
    "VM Hostname": ("Text", False, False),
    "VM Proxmox Node": ("Text", True, False),
    "VM Template ID": ("Int", True, False),
    "VM NetBox Record ID": ("Text", True, False),
    "VM Provisioning Error": ("Text", True, False),
    "Provisioning State": ("Text", False, False),
}


def resource_attribute_type(name, kind):
    value_type, _ = AttributeType.objects.get_or_create(name=kind)
    row, _ = ResourceAttributeType.objects.update_or_create(
        name=name,
        defaults={
            "attribute_type": value_type,
            "is_required": False,
            "is_unique_per_resource": True,
            "is_value_unique": False,
        },
    )
    return row


def resource_values(configuration):
    directory_login = (
        "Selected active allocation users; site guest policy applies"
        if configuration.guest_access_enabled
        else "Not managed by this provisioner"
    )
    return {
        "Operating System": ("Text", configuration.operating_system),
        "SSH Login User": ("Text", configuration.guest_username),
        "Directory Login": ("Text", directory_login),
        "Service Term": (
            "Text",
            f"{configuration.service_term_months} months; renewal requires staff review",
        ),
        "allocation_limit": ("Int", str(configuration.allocation_limit)),
        "form_description": (
            "Text",
            (
                f"Request a {configuration.operating_system} VM with project ownership and a "
                f"policy-managed {configuration.service_term_months}-month term."
            ),
        ),
    }


class Command(BaseCommand):
    help = "Synchronize and validate the ColdFront catalog from PVE Provisioner admin policy."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--check", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        configuration = get_configuration()
        values = resource_values(configuration)
        resource_fields = {
            "description": configuration.resource_description,
            "is_allocatable": True,
            "is_available": True,
            "is_public": True,
            "requires_payment": False,
        }
        if options["apply"]:
            resource_type, _ = ResourceType.objects.update_or_create(
                name=RESOURCE_TYPE_NAME,
                defaults={"description": "Project-owned virtual infrastructure"},
            )
            resource, _ = Resource.objects.update_or_create(
                name=configuration.resource_name,
                defaults={"resource_type": resource_type, **resource_fields},
            )
            if resource.resource_type_id != resource_type.pk:
                raise CommandError(
                    "The existing PVE resource has an unexpected resource type."
                )
            for field, value in resource_fields.items():
                setattr(resource, field, value)
            resource.save(update_fields=[*resource_fields, "modified"])
            for name, (kind, value) in values.items():
                ResourceAttribute.objects.update_or_create(
                    resource=resource,
                    resource_attribute_type=resource_attribute_type(name, kind),
                    defaults={"value": value},
                )
            for name, (
                kind,
                is_private,
                is_changeable,
            ) in ALLOCATION_ATTRIBUTES.items():
                value_type, _ = AllocationValueType.objects.get_or_create(name=kind)
                AllocationAttributeType.objects.update_or_create(
                    name=name,
                    defaults={
                        "attribute_type": value_type,
                        "has_usage": False,
                        "is_required": False,
                        "is_unique": False,
                        "is_private": is_private,
                        "is_changeable": is_changeable,
                    },
                )
            Schedule.objects.update_or_create(
                name=SCHEDULE_NAME,
                defaults={
                    "func": "coldfront_pve_provisioner.tasks.dispatch_queued_jobs",
                    "schedule_type": Schedule.MINUTES,
                    "minutes": 1,
                    "repeats": -1,
                },
            )
            if configuration.retirement_enabled:
                Schedule.objects.update_or_create(
                    name="PVE VM Provisioner retirement-backup cleanup",
                    defaults={
                        "func": "coldfront_pve_provisioner.tasks.cleanup_expired_retirement_backups",
                        "schedule_type": Schedule.DAILY,
                        "repeats": -1,
                    },
                )
            VMIdentityPool.objects.get_or_create(singleton=1)

        errors = []
        if not ProvisionerFlavor.objects.filter(enabled=True).exists():
            errors.append("no enabled VM flavor is configured")
        resource = Resource.objects.filter(name=configuration.resource_name).first()
        if not resource:
            errors.append(f"resource {configuration.resource_name!r} is absent")
        else:
            for field, expected in resource_fields.items():
                if getattr(resource, field) != expected:
                    errors.append(f"resource.{field} differs")
            for name, (_kind, expected) in values.items():
                if resource.get_attribute(name, typed=False) != expected:
                    errors.append(f"resource attribute {name!r} differs")
        for name, (_kind, is_private, is_changeable) in ALLOCATION_ATTRIBUTES.items():
            row = AllocationAttributeType.objects.filter(name=name).first()
            if (
                not row
                or row.is_private != is_private
                or row.is_changeable != is_changeable
            ):
                errors.append(
                    f"allocation attribute {name!r} is absent or has incorrect policy"
                )
        if not Schedule.objects.filter(
            name=SCHEDULE_NAME,
            func="coldfront_pve_provisioner.tasks.dispatch_queued_jobs",
            schedule_type=Schedule.MINUTES,
            minutes=1,
            repeats=-1,
        ).exists():
            errors.append("Django-Q dispatcher schedule is absent or incorrect")
        if errors:
            raise CommandError("; ".join(errors))
        self.stdout.write(
            self.style.SUCCESS("PVE VM resource and dispatcher are configured.")
        )
