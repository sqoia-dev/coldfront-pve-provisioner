from django.db import migrations, models
from django.db.models import Q


def mark_legacy_retired_backups_released(apps, schema_editor):
    vm = apps.get_model("coldfront_pve_provisioner", "VirtualMachine")
    vm.objects.filter(state="Retired", retirement_backup_volume="").update(
        retirement_backup_deleted_at=models.F("retired_at")
    )


class Migration(migrations.Migration):
    dependencies = [("coldfront_pve_provisioner", "0004_virtualmachine_access_sync")]

    operations = [
        migrations.AddField(
            model_name="virtualmachine",
            name="retirement_backup_created_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="virtualmachine",
            name="retirement_backup_deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="virtualmachine",
            name="retirement_backup_expires_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="virtualmachine",
            name="retirement_backup_upid",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="virtualmachine",
            name="retirement_backup_volume",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.RunPython(
            mark_legacy_retired_backups_released, migrations.RunPython.noop
        ),
        migrations.RemoveConstraint(
            model_name="virtualmachine", name="pve_one_live_vmid"
        ),
        migrations.RemoveConstraint(
            model_name="virtualmachine", name="pve_one_live_ipv4"
        ),
        migrations.AddConstraint(
            model_name="virtualmachine",
            constraint=models.UniqueConstraint(
                condition=Q(retirement_backup_deleted_at__isnull=True),
                fields=("vmid",),
                name="pve_one_live_vmid",
            ),
        ),
        migrations.AddConstraint(
            model_name="virtualmachine",
            constraint=models.UniqueConstraint(
                condition=Q(retirement_backup_deleted_at__isnull=True),
                fields=("ipv4_address",),
                name="pve_one_live_ipv4",
            ),
        ),
    ]
