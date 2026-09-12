import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("coldfront_pve_provisioner", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="VMRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("ssh_public_key", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "allocation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pve_provisioner_request",
                        to="allocation.allocation",
                    ),
                ),
            ],
        ),
    ]
