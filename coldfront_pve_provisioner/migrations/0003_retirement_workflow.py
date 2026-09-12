from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("coldfront_pve_provisioner", "0002_vmrequest")]

    operations = [
        migrations.AlterField(
            model_name="virtualmachine",
            name="vmid",
            field=models.PositiveSmallIntegerField(),
        ),
        migrations.AlterField(
            model_name="virtualmachine",
            name="ipv4_address",
            field=models.GenericIPAddressField(protocol="IPv4"),
        ),
        migrations.AddField(
            model_name="virtualmachine",
            name="pve_deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="virtualmachine",
            name="netbox_deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="virtualmachine",
            name="retired_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="virtualmachine",
            name="state",
            field=models.CharField(
                choices=[
                    ("Reserved", "Reserved"),
                    ("Queued", "Queued"),
                    ("Provisioning", "Provisioning"),
                    ("Starting", "Starting"),
                    ("Active", "Active"),
                    ("Blocked", "Blocked"),
                    ("Failed", "Failed"),
                    ("Retirement Review", "Retirement review required"),
                    ("Retiring", "Retiring"),
                    ("Retired", "Retired"),
                ],
                default="Reserved",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="provisioningjob",
            name="action",
            field=models.CharField(
                choices=[
                    ("Provision", "Provision"),
                    ("Reconcile", "Reconcile"),
                    ("Retire", "Retire"),
                ],
                default="Provision",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="virtualmachine",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "Retired"), _negated=True),
                fields=("vmid",),
                name="pve_one_live_vmid",
            ),
        ),
        migrations.AddConstraint(
            model_name="virtualmachine",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "Retired"), _negated=True),
                fields=("ipv4_address",),
                name="pve_one_live_ipv4",
            ),
        ),
    ]
