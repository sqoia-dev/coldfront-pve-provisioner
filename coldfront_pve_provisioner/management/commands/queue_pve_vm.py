from django.core.management.base import BaseCommand, CommandError

from ...services import create_provisioning_job, dispatch_job


class Command(BaseCommand):
    help = (
        "Idempotently reserve and queue one approved ColdFront Proxmox VM allocation."
    )

    def add_arguments(self, parser):
        parser.add_argument("allocation_id", type=int)
        parser.add_argument("--dispatch", action="store_true")

    def handle(self, *args, **options):
        job = create_provisioning_job(options["allocation_id"])
        if not job:
            raise CommandError("Allocation is not an active Proxmox VM allocation.")
        if options["dispatch"]:
            dispatch_job(job)
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued job {job.pk} for VMID {job.virtual_machine.vmid}."
            )
        )
