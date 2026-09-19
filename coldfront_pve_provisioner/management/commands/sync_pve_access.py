from django.core.management.base import BaseCommand, CommandError

from ...models import VirtualMachine
from ...services import create_access_reconciliation_job, dispatch_job


class Command(BaseCommand):
    help = (
        "Queue declarative guest policy and exact allocation-user access "
        "reconciliation for one managed VM."
    )

    def add_arguments(self, parser):
        parser.add_argument("allocation_id", type=int)
        parser.add_argument("--dispatch", action="store_true")

    def handle(self, *args, **options):
        allocation_id = options["allocation_id"]
        if not VirtualMachine.objects.filter(allocation_id=allocation_id).exists():
            raise CommandError("Allocation has no managed Proxmox VM.")
        job = create_access_reconciliation_job(allocation_id)
        if not job:
            self.stdout.write(
                self.style.SUCCESS(
                    "VM guest policy and directory access are already synchronized, "
                    "disabled, or owned by provisioning."
                )
            )
            return
        if options["dispatch"] and not job.django_q_task_id:
            dispatch_job(job)
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued guest-reconciliation job {job.pk} for VMID {job.virtual_machine.vmid}."
            )
        )
