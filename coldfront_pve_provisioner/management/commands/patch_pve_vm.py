from django.core.management.base import BaseCommand, CommandError

from ...services import create_guest_patch_job, dispatch_job


class Command(BaseCommand):
    help = "Queue configured package patching and guest-policy reconciliation for one managed VM."

    def add_arguments(self, parser):
        parser.add_argument("allocation_id", type=int)
        parser.add_argument("--dispatch", action="store_true")

    def handle(self, *args, **options):
        job = create_guest_patch_job(options["allocation_id"])
        if job is None:
            raise CommandError(
                "No patch job was created; verify the allocation, active VM state, "
                "declarative guest policy, and patch policy."
            )
        if options["dispatch"] and not job.django_q_task_id:
            dispatch_job(job)
        self.stdout.write(str(job.pk))
