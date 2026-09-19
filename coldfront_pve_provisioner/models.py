import uuid
from ipaddress import ip_address, ip_network

from coldfront.core.allocation.models import Allocation
from django.core.exceptions import ValidationError
from django.db import models

from .constants import hostname_for, ipv4_for_vmid
from .guest_policy import (
    normalize_packages,
    normalize_service_units,
    validate_file_identity,
    validate_managed_file_content,
    validate_managed_file_path,
)
from .validators import validate_ssh_public_key


class ProvisionerConfiguration(models.Model):
    """Non-secret site policy editable by administrators."""

    class GuestPackageManager(models.TextChoices):
        NONE = "none", "Do not manage packages"
        DNF = "dnf", "DNF (RHEL, Rocky, Alma, Fedora)"
        APT = "apt", "APT (Debian, Ubuntu)"

    class GuestPatchMode(models.TextChoices):
        NONE = "none", "Do not patch"
        SECURITY = "security", "Security updates only"
        ALL = "all", "All available updates"

    singleton = models.PositiveSmallIntegerField(
        primary_key=True, default=1, editable=False
    )
    enabled = models.BooleanField(default=False)
    resource_name = models.CharField(max_length=100, default="Proxmox Virtual Machine")
    resource_description = models.TextField(
        default="Self-service virtual machines provisioned on Proxmox VE."
    )
    allocation_limit = models.PositiveSmallIntegerField(default=1)
    service_term_months = models.PositiveSmallIntegerField(default=6)
    operating_system = models.CharField(max_length=100, default="Linux cloud image")
    guest_username = models.CharField(max_length=32, default="cloud-user")

    vmid_min = models.PositiveIntegerField("VMID minimum", default=1100)
    vmid_max = models.PositiveIntegerField("VMID maximum", default=1199)
    ipv4_pool_start = models.GenericIPAddressField(
        "IPv4 pool start",
        protocol="IPv4",
        default="192.0.2.100",
        help_text="First usable IPv4 address in the built-in allocation pool.",
    )
    ipv4_pool_end = models.GenericIPAddressField(
        "IPv4 pool end",
        protocol="IPv4",
        default="192.0.2.199",
        help_text="Last usable IPv4 address in the built-in allocation pool.",
    )
    network_prefix_length = models.PositiveSmallIntegerField(
        "IPv4 prefix length",
        default=24,
        help_text="IPv4 CIDR prefix length applied to provisioned VMs.",
    )
    gateway = models.GenericIPAddressField(
        "IPv4 gateway",
        protocol="IPv4",
        default="192.0.2.1",
        help_text="Default IPv4 gateway; it must not be inside the allocation pool.",
    )
    nameservers = models.TextField(
        "DNS nameservers",
        default="1.1.1.1\n1.0.0.1",
        help_text="One IPv4 address per line.",
    )
    dns_search_domain = models.CharField(
        "DNS search domain",
        max_length=253,
        default="example.org",
        help_text="DNS search domain and default suffix available to the hostname template.",
    )
    hostname_template = models.CharField(
        max_length=253,
        default="coldfront-a{allocation_id}-v{vmid}.{domain}",
        help_text="Available fields: {allocation_id}, {vmid}, and {domain}.",
    )

    template_vmid = models.PositiveIntegerField(default=9000)
    template_name = models.CharField(
        max_length=128, default="coldfront-linux-cloud-template"
    )
    storage = models.CharField(max_length=64, default="local-lvm")
    bridge = models.CharField(max_length=64, default="vmbr0")
    cpu_type = models.CharField(max_length=64, default="x86-64-v2-AES")
    allowed_nodes = models.TextField(help_text="One PVE node name per line.")
    proxmox_pool = models.CharField(max_length=64, default="coldfront-managed")
    cloud_init_vendor_snippet = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional PVE storage snippet reference, such as local:snippets/site.yml.",
    )

    netbox_enabled = models.BooleanField(
        "NetBox inventory mirroring",
        default=False,
        help_text=(
            "Mirror VM, interface, and IP records to NetBox. The built-in pool remains "
            "authoritative."
        ),
    )
    netbox_cluster_type = models.CharField(
        "NetBox cluster type", max_length=64, default="Proxmox VE"
    )
    netbox_cluster_name = models.CharField(
        "NetBox cluster name", max_length=100, default="coldfront-pve"
    )
    netbox_managed_tag = models.SlugField(
        "NetBox managed tag", max_length=100, default="coldfront-managed"
    )

    guest_access_enabled = models.BooleanField(default=False)
    guest_access_group = models.CharField(max_length=64, default="coldfront-vm-access")
    guest_user_group = models.CharField(max_length=64, default="coldfront-vm-users")
    guest_access_helper = models.CharField(
        max_length=255, default="/usr/local/libexec/coldfront-vm-access-reconcile"
    )

    guest_policy_enabled = models.BooleanField(
        "Declarative guest policy",
        default=False,
        help_text=(
            "Apply configured packages, files, and services through the versioned "
            "guest helper contract."
        ),
    )
    guest_policy_helper = models.CharField(
        "Guest policy helper",
        max_length=255,
        default="/usr/local/libexec/coldfront-guest-reconcile",
        help_text="Absolute path to the reviewed in-guest helper installed by the image or cloud-init.",
    )
    guest_package_manager = models.CharField(
        "Package manager",
        max_length=8,
        choices=GuestPackageManager.choices,
        default=GuestPackageManager.NONE,
    )
    guest_packages = models.TextField(
        "Packages",
        blank=True,
        help_text="One package name per line. Shell commands and arguments are not accepted.",
    )
    guest_service_units = models.TextField(
        "Systemd units",
        blank=True,
        help_text="One .service, .socket, or .timer unit per line.",
    )
    guest_enable_services = models.BooleanField(
        "Enable configured services",
        default=True,
        help_text="Enable and start configured units after package or file reconciliation.",
    )
    guest_reconcile_on_membership_change = models.BooleanField(
        "Reconcile when allocation membership changes",
        default=True,
        help_text=(
            "Queue guest policy after active allocation users are added or removed. "
            "Existing directory-access reconciliation remains independently configurable."
        ),
    )
    guest_patch_mode = models.CharField(
        "Patch policy",
        max_length=8,
        choices=GuestPatchMode.choices,
        default=GuestPatchMode.NONE,
    )
    guest_patch_interval_days = models.PositiveSmallIntegerField(
        "Patch interval (days)",
        default=0,
        help_text="Zero disables scheduled patch jobs. Manual patch jobs use the same policy.",
    )

    retirement_enabled = models.BooleanField(default=False)
    retirement_backup_storage = models.CharField(max_length=64, blank=True)
    retirement_backup_retention_days = models.PositiveSmallIntegerField(default=30)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "PVE provisioner configuration"
        verbose_name_plural = "PVE provisioner configuration"

    def __str__(self):
        return self.resource_name

    @property
    def nameserver_list(self):
        return tuple(
            line.strip() for line in self.nameservers.splitlines() if line.strip()
        )

    @property
    def allowed_node_list(self):
        return tuple(
            line.strip() for line in self.allowed_nodes.splitlines() if line.strip()
        )

    @property
    def guest_package_list(self):
        return normalize_packages(self.guest_packages)

    @property
    def guest_service_unit_list(self):
        return normalize_service_units(self.guest_service_units)

    def clean(self):
        errors = {}
        if self.singleton != 1:
            errors["singleton"] = "Only the singleton configuration row is supported."
        if self.vmid_min > self.vmid_max:
            errors["vmid_max"] = "Must be greater than or equal to the minimum VMID."
        try:
            start = ip_address(self.ipv4_pool_start)
            end = ip_address(self.ipv4_pool_end)
            gateway = ip_address(self.gateway)
            if not 0 <= self.network_prefix_length <= 32:
                errors["network_prefix_length"] = "Must be between 0 and 32."
            network = ip_network(f"{start}/{self.network_prefix_length}", strict=False)
            if start > end:
                errors["ipv4_pool_end"] = "Must not precede the pool start."
            elif int(end) - int(start) < self.vmid_max - self.vmid_min:
                errors["ipv4_pool_end"] = (
                    "The IPv4 pool must contain one address for every VMID."
                )
            if start not in network or end not in network or gateway not in network:
                errors["ipv4_pool_start"] = (
                    "Pool and gateway must share the configured subnet."
                )
            elif start == network.network_address or end == network.broadcast_address:
                errors["ipv4_pool_start"] = (
                    "The allocation pool must exclude the subnet network and broadcast addresses."
                )
            elif start <= gateway <= end:
                errors["gateway"] = (
                    "The gateway must not be inside the allocation pool."
                )
        except ValueError as exc:
            errors["ipv4_pool_start"] = str(exc)
        for value in self.nameserver_list:
            try:
                nameserver = ip_address(value)
                if nameserver.version != 4:
                    raise ValueError("IPv6 is not supported by this IPv4 pool.")
            except ValueError:
                errors["nameservers"] = f"Invalid nameserver address: {value}"
        if not self.allowed_node_list:
            errors["allowed_nodes"] = "At least one PVE node is required."
        try:
            rendered = self.hostname_template.format(
                allocation_id=1, vmid=self.vmid_min, domain=self.dns_search_domain
            ).rstrip(".")
            if not rendered or len(rendered) > 253:
                errors["hostname_template"] = (
                    "The rendered hostname must be 1-253 characters."
                )
        except (KeyError, ValueError) as exc:
            errors["hostname_template"] = f"Invalid hostname template: {exc}"
        if self.guest_access_enabled and not self.cloud_init_vendor_snippet:
            errors["cloud_init_vendor_snippet"] = (
                "Required when the legacy directory-access adapter is enabled."
            )
        try:
            packages = self.guest_package_list
        except ValueError as exc:
            errors["guest_packages"] = str(exc)
            packages = ()
        try:
            normalize_service_units(self.guest_service_units)
        except ValueError as exc:
            errors["guest_service_units"] = str(exc)
        if packages and self.guest_package_manager == self.GuestPackageManager.NONE:
            errors["guest_package_manager"] = (
                "Choose DNF or APT when packages are configured."
            )
        if self.guest_patch_mode != self.GuestPatchMode.NONE:
            if self.guest_package_manager == self.GuestPackageManager.NONE:
                errors["guest_patch_mode"] = (
                    "Patching requires a configured package manager."
                )
            if (
                self.guest_package_manager == self.GuestPackageManager.APT
                and self.guest_patch_mode == self.GuestPatchMode.SECURITY
            ):
                errors["guest_patch_mode"] = (
                    "Security-only patching is not portable through APT; use all updates or disable patching."
                )
        if (
            self.guest_patch_interval_days
            and self.guest_patch_mode == self.GuestPatchMode.NONE
        ):
            errors["guest_patch_interval_days"] = (
                "Scheduled patching requires a non-disabled patch policy."
            )
        for field_name in ("guest_policy_helper", "guest_access_helper"):
            helper = getattr(self, field_name)
            if not helper.startswith("/") or any(char.isspace() for char in helper):
                errors[field_name] = (
                    "Guest helper paths must be absolute and contain no whitespace."
                )
        if self.retirement_enabled and not self.retirement_backup_storage:
            errors["retirement_backup_storage"] = (
                "Required when guarded retirement is enabled."
            )
        original = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if original is not None and VirtualMachine.objects.exists():
            mutable_after_use = {
                "allocation_limit",
                "enabled",
                "guest_access_enabled",
                "guest_access_group",
                "guest_access_helper",
                "guest_enable_services",
                "guest_package_manager",
                "guest_packages",
                "guest_patch_interval_days",
                "guest_patch_mode",
                "guest_policy_enabled",
                "guest_policy_helper",
                "guest_reconcile_on_membership_change",
                "guest_service_units",
                "guest_user_group",
                "resource_description",
            }
            changed = [
                field.name
                for field in self._meta.fields
                if field.name not in mutable_after_use | {"singleton", "updated_at"}
                and getattr(original, field.name) != getattr(self, field.name)
            ]
            if changed:
                errors["__all__"] = (
                    "Identity and lifecycle policy is frozen after the first VM reservation; "
                    f"changed fields: {', '.join(changed)}."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "The singleton provisioner configuration must be disabled, not deleted."
        )


class ProvisionerFlavor(models.Model):
    code = models.SlugField(max_length=32, unique=True)
    label = models.CharField(max_length=100)
    cores = models.PositiveSmallIntegerField()
    memory_mib = models.PositiveIntegerField()
    disk_gib = models.PositiveIntegerField()
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "code")

    def __str__(self):
        return self.label

    def clean(self):
        if not self.pk:
            return
        original = type(self).objects.filter(pk=self.pk).first()
        if (
            original is None
            or not VirtualMachine.objects.filter(flavor=original.code).exists()
        ):
            return
        immutable = ("code", "cores", "memory_mib", "disk_gib")
        changed = [
            field
            for field in immutable
            if getattr(original, field) != getattr(self, field)
        ]
        if changed:
            raise ValidationError(
                "A flavor's code and dimensions are frozen after use; "
                f"changed fields: {', '.join(changed)}."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if VirtualMachine.objects.filter(flavor=self.code).exists():
            raise ValidationError(
                "A flavor referenced by a VM cannot be deleted; disable it instead."
            )
        return super().delete(*args, **kwargs)


class GuestManagedFile(models.Model):
    configuration = models.ForeignKey(
        ProvisionerConfiguration,
        on_delete=models.CASCADE,
        related_name="guest_managed_files",
    )
    path = models.CharField(
        max_length=255,
        help_text="Absolute destination beneath /etc, /opt, or /usr/local.",
    )
    content_template = models.TextField(
        help_text=(
            "Non-secret UTF-8 content. Supported variables: allocation_id, vmid, "
            "hostname, ipv4_address, allocation_users_lines, allocation_users_json, "
            "and allocation_users_ldap_filter."
        )
    )
    owner = models.CharField(max_length=32, default="root")
    group = models.CharField(max_length=32, default="root")
    mode = models.CharField(max_length=4, default="0644")
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "path")
        constraints = (
            models.UniqueConstraint(
                fields=("configuration", "path"),
                name="pve_one_guest_managed_path",
            ),
        )
        verbose_name = "guest managed file"
        verbose_name_plural = "guest managed files"

    def __str__(self):
        return self.path

    def clean(self):
        errors = {}
        try:
            self.path = validate_managed_file_path(self.path)
        except ValueError as exc:
            errors["path"] = str(exc)
        try:
            validate_managed_file_content(self.content_template)
        except ValueError as exc:
            errors["content_template"] = str(exc)
        try:
            validate_file_identity(self.owner, self.group, self.mode)
        except ValueError as exc:
            errors["__all__"] = str(exc)
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


def get_configuration(*, require_enabled=True):
    try:
        configuration = ProvisionerConfiguration.objects.get(singleton=1)
    except ProvisionerConfiguration.DoesNotExist as exc:
        raise RuntimeError(
            "PVE VM Provisioner is not configured; create its singleton configuration in Django admin."
        ) from exc
    configuration.full_clean()
    if require_enabled and not configuration.enabled:
        raise RuntimeError("PVE VM Provisioner is disabled in Django admin.")
    return configuration


def get_flavor(code):
    try:
        return ProvisionerFlavor.objects.get(code=code, enabled=True)
    except ProvisionerFlavor.DoesNotExist as exc:
        raise ValidationError("Choose an enabled VM flavor.") from exc


class VMIdentityPool(models.Model):
    """Singleton row used to serialize identity reservations."""

    singleton = models.PositiveSmallIntegerField(
        primary_key=True, default=1, editable=False
    )
    modified = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.singleton != 1:
            raise ValidationError("The VM identity pool must use singleton key 1.")

    def __str__(self):
        return "PVE VM identity pool"


class VMRequest(models.Model):
    """Request material that does not fit ColdFront's 128-byte attributes."""

    allocation = models.OneToOneField(
        Allocation,
        on_delete=models.PROTECT,
        related_name="pve_provisioner_request",
    )
    ssh_public_key = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"VM request for allocation {self.allocation_id}"

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if (
                original.allocation_id != self.allocation_id
                or original.ssh_public_key != self.ssh_public_key
            ):
                raise ValidationError(
                    "The submitted allocation identity and SSH public key are immutable."
                )
        self.ssh_public_key = validate_ssh_public_key(self.ssh_public_key)
        return super().save(*args, **kwargs)


class VirtualMachine(models.Model):
    class State(models.TextChoices):
        RESERVED = "Reserved", "Reserved"
        QUEUED = "Queued", "Queued"
        PROVISIONING = "Provisioning", "Provisioning"
        STARTING = "Starting", "Starting"
        ACTIVE = "Active", "Active"
        BLOCKED = "Blocked", "Blocked"
        FAILED = "Failed", "Failed"
        RETIREMENT_REVIEW = "Retirement Review", "Retirement review required"
        RETIRING = "Retiring", "Retiring"
        RETIRED = "Retired", "Retired"

    allocation = models.OneToOneField(
        Allocation,
        on_delete=models.PROTECT,
        related_name="pve_provisioned_vm",
    )
    vmid = models.PositiveSmallIntegerField()
    ipv4_address = models.GenericIPAddressField(protocol="IPv4")
    hostname = models.CharField(max_length=253, unique=True)
    flavor = models.CharField(max_length=32)
    target_node = models.CharField(max_length=64, blank=True)
    template_vmid = models.PositiveSmallIntegerField()
    netbox_vm_id = models.PositiveIntegerField(null=True, blank=True, unique=True)
    netbox_interface_id = models.PositiveIntegerField(
        null=True, blank=True, unique=True
    )
    netbox_ip_id = models.PositiveIntegerField(null=True, blank=True, unique=True)
    state = models.CharField(
        max_length=32, choices=State.choices, default=State.RESERVED
    )
    last_error = models.TextField(blank=True)
    access_desired_users = models.JSONField(default=list, blank=True)
    access_applied_users = models.JSONField(default=list, blank=True)
    access_last_error = models.TextField(blank=True)
    access_synced_at = models.DateTimeField(null=True, blank=True)
    guest_policy_hash = models.CharField(max_length=64, blank=True)
    guest_policy_last_error = models.TextField(blank=True)
    guest_policy_applied_at = models.DateTimeField(null=True, blank=True)
    guest_patched_at = models.DateTimeField(null=True, blank=True)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    pve_deleted_at = models.DateTimeField(null=True, blank=True)
    netbox_deleted_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    retirement_backup_upid = models.CharField(max_length=255, blank=True)
    retirement_backup_volume = models.CharField(max_length=255, blank=True)
    retirement_backup_created_at = models.DateTimeField(null=True, blank=True)
    retirement_backup_expires_at = models.DateTimeField(
        null=True, blank=True, db_index=True
    )
    retirement_backup_deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("vmid",)
        constraints = (
            models.UniqueConstraint(
                fields=("vmid",),
                condition=models.Q(retirement_backup_deleted_at__isnull=True),
                name="pve_one_live_vmid",
            ),
            models.UniqueConstraint(
                fields=("ipv4_address",),
                condition=models.Q(retirement_backup_deleted_at__isnull=True),
                name="pve_one_live_ipv4",
            ),
        )

    def __str__(self):
        return f"{self.hostname} (VMID {self.vmid}, {self.ipv4_address})"

    @property
    def ip_reservation_status(self):
        if self.retirement_backup_deleted_at is not None:
            return "Released"
        if self.state == self.State.RETIRED:
            return "Held for recovery"
        return "Reserved"

    def clean(self):
        configuration = get_configuration()
        if not configuration.vmid_min <= self.vmid <= configuration.vmid_max:
            raise ValidationError(
                {
                    "vmid": f"VMID must be in {configuration.vmid_min}-{configuration.vmid_max}."
                }
            )
        if self.ipv4_address != ipv4_for_vmid(configuration, self.vmid):
            raise ValidationError(
                {
                    "ipv4_address": "IPv4 address does not match the deterministic VMID mapping."
                }
            )
        if self.allocation_id and self.hostname != hostname_for(
            configuration, self.allocation_id, self.vmid
        ):
            raise ValidationError(
                {
                    "hostname": "Hostname must reference the ColdFront allocation and VMID."
                }
            )

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            immutable = (
                "allocation_id",
                "vmid",
                "ipv4_address",
                "hostname",
                "template_vmid",
            )
            if any(
                getattr(original, field) != getattr(self, field) for field in immutable
            ):
                raise ValidationError(
                    "Reserved allocation, VMID, IP, hostname, and template identity are immutable."
                )
        self.full_clean()
        return super().save(*args, **kwargs)


class ProvisioningJob(models.Model):
    class Action(models.TextChoices):
        PROVISION = "Provision", "Provision"
        RECONCILE = "Reconcile", "Reconcile guest"
        PATCH = "Patch", "Patch guest"
        RETIRE = "Retire", "Retire"

    class Status(models.TextChoices):
        QUEUED = "Queued", "Queued"
        RUNNING = "Running", "Running"
        SUCCEEDED = "Succeeded", "Succeeded"
        BLOCKED = "Blocked", "Blocked"
        FAILED = "Failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    virtual_machine = models.ForeignKey(
        VirtualMachine,
        on_delete=models.PROTECT,
        related_name="provisioning_jobs",
    )
    action = models.CharField(
        max_length=16, choices=Action.choices, default=Action.PROVISION
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    django_q_task_id = models.CharField(max_length=64, blank=True)
    external_upid = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-queued_at",)

    def __str__(self):
        return f"{self.action} {self.virtual_machine} [{self.status}]"


class ProvisioningEvent(models.Model):
    virtual_machine = models.ForeignKey(
        VirtualMachine,
        on_delete=models.PROTECT,
        related_name="events",
    )
    job = models.ForeignKey(
        ProvisioningJob,
        on_delete=models.PROTECT,
        related_name="events",
        null=True,
        blank=True,
    )
    event_type = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-occurred_at", "-pk")

    def __str__(self):
        return f"{self.event_type} — {self.virtual_machine}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Provisioning events are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Provisioning events are retained as audit records.")


class IPAddressReservation(VirtualMachine):
    """Read-only admin projection of built-in IPv4 pool reservations."""

    class Meta:
        proxy = True
        verbose_name = "IP address reservation"
        verbose_name_plural = "IP address reservations"
