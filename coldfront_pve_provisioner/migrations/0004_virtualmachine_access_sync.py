from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("coldfront_pve_provisioner", "0003_retirement_workflow")]

    operations = [
        migrations.AddField(
            model_name="virtualmachine",
            name="access_applied_users",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="virtualmachine",
            name="access_desired_users",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="virtualmachine",
            name="access_last_error",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="virtualmachine",
            name="access_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
