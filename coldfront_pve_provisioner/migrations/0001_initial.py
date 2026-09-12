import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [("allocation", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="VMIdentityPool",
            fields=[
                (
                    "singleton",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("modified", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="VirtualMachine",
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
                ("vmid", models.PositiveSmallIntegerField(unique=True)),
                (
                    "ipv4_address",
                    models.GenericIPAddressField(protocol="IPv4", unique=True),
                ),
                ("hostname", models.CharField(max_length=253, unique=True)),
                ("flavor", models.CharField(max_length=32)),
                ("target_node", models.CharField(blank=True, max_length=64)),
                ("template_vmid", models.PositiveSmallIntegerField()),
                (
                    "netbox_vm_id",
                    models.PositiveIntegerField(blank=True, null=True, unique=True),
                ),
                (
                    "netbox_interface_id",
                    models.PositiveIntegerField(blank=True, null=True, unique=True),
                ),
                (
                    "netbox_ip_id",
                    models.PositiveIntegerField(blank=True, null=True, unique=True),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("Reserved", "Reserved"),
                            ("Queued", "Queued"),
                            ("Provisioning", "Provisioning"),
                            ("Starting", "Starting"),
                            ("Active", "Active"),
                            ("Blocked", "Blocked"),
                            ("Failed", "Failed"),
                            ("Retirement Review", "Retirement review required"),
                            ("Retired", "Retired"),
                        ],
                        default="Reserved",
                        max_length=32,
                    ),
                ),
                ("last_error", models.TextField(blank=True)),
                ("provisioned_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "allocation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pve_provisioned_vm",
                        to="allocation.allocation",
                    ),
                ),
            ],
            options={"ordering": ("vmid",)},
        ),
        migrations.CreateModel(
            name="ProvisioningJob",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("Provision", "Provision"),
                            ("Reconcile", "Reconcile"),
                        ],
                        default="Provision",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("Queued", "Queued"),
                            ("Running", "Running"),
                            ("Succeeded", "Succeeded"),
                            ("Blocked", "Blocked"),
                            ("Failed", "Failed"),
                        ],
                        default="Queued",
                        max_length=16,
                    ),
                ),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("django_q_task_id", models.CharField(blank=True, max_length=64)),
                ("external_upid", models.CharField(blank=True, max_length=255)),
                ("error", models.TextField(blank=True)),
                ("queued_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "virtual_machine",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="provisioning_jobs",
                        to="coldfront_pve_provisioner.virtualmachine",
                    ),
                ),
            ],
            options={"ordering": ("-queued_at",)},
        ),
        migrations.CreateModel(
            name="ProvisioningEvent",
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
                ("event_type", models.CharField(max_length=64)),
                ("occurred_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="coldfront_pve_provisioner.provisioningjob",
                    ),
                ),
                (
                    "virtual_machine",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="coldfront_pve_provisioner.virtualmachine",
                    ),
                ),
            ],
            options={"ordering": ("-occurred_at", "-pk")},
        ),
    ]
