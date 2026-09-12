from django.core.management.base import BaseCommand, CommandError

from ...services import create_retirement_job, dispatch_job


class Command(BaseCommand):
    help = (
        "Queue exact-identity retirement for one ended ColdFront Proxmox VM allocation."
    )

    def add_arguments(self, parser):
        parser.add_argument("allocation_id", type=int)
        parser.add_argument("--confirm-allocation", type=int, required=True)
        parser.add_argument("--dispatch", action="store_true")

    def handle(self, *args, **options):
        allocation_id = options["allocation_id"]
        if options["confirm_allocation"] != allocation_id:
            raise CommandError(
                "The exact allocation confirmation does not match; refusing retirement."
            )
        job = create_retirement_job(allocation_id)
        if not job:
            raise CommandError("The allocation has no non-retired Proxmox VM.")
        if options["dispatch"]:
            dispatch_job(job)
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued retirement job {job.pk} for allocation {allocation_id}, VMID {job.virtual_machine.vmid}."
            )
        )
