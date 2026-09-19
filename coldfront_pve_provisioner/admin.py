from django.contrib import admin

from .models import (
    IPAddressReservation,
    ProvisionerConfiguration,
    ProvisionerFlavor,
    ProvisioningEvent,
    ProvisioningJob,
    VirtualMachine,
)


@admin.register(ProvisionerConfiguration)
class ProvisionerConfigurationAdmin(admin.ModelAdmin):
    fieldsets = (
        (
            "Activation and catalog",
            {
                "fields": (
                    "enabled",
                    "resource_name",
                    "resource_description",
                    "allocation_limit",
                    "service_term_months",
                    "operating_system",
                    "guest_username",
                )
            },
        ),
        (
            "Built-in identity and IPv4 allocation",
            {
                "fields": (
                    "vmid_min",
                    "vmid_max",
                    "ipv4_pool_start",
                    "ipv4_pool_end",
                    "ipv4_pool_capacity",
                    "network_prefix_length",
                    "gateway",
                    "nameservers",
                    "dns_search_domain",
                    "hostname_template",
                )
            },
        ),
        (
            "Proxmox VE",
            {
                "fields": (
                    "template_vmid",
                    "template_name",
                    "storage",
                    "bridge",
                    "cpu_type",
                    "allowed_nodes",
                    "proxmox_pool",
                    "cloud_init_vendor_snippet",
                )
            },
        ),
        (
            "Optional NetBox inventory mirror",
            {
                "fields": (
                    "netbox_enabled",
                    "netbox_cluster_type",
                    "netbox_cluster_name",
                    "netbox_managed_tag",
                )
            },
        ),
        (
            "Optional guest access",
            {
                "fields": (
                    "guest_access_enabled",
                    "guest_access_group",
                    "guest_user_group",
                    "guest_access_helper",
                )
            },
        ),
        (
            "Guarded retirement",
            {
                "fields": (
                    "retirement_enabled",
                    "retirement_backup_storage",
                    "retirement_backup_retention_days",
                )
            },
        ),
    )
    readonly_fields = ("ipv4_pool_capacity", "updated_at")

    @admin.display(description="Pool capacity")
    def ipv4_pool_capacity(self, obj):
        if obj is None:
            return "Save the configuration to calculate capacity."
        try:
            from ipaddress import ip_address

            count = (
                int(ip_address(obj.ipv4_pool_end))
                - int(ip_address(obj.ipv4_pool_start))
                + 1
            )
        except ValueError:
            return "Invalid range"
        return f"{count} addresses mapped one-to-one to VMIDs"

    def has_add_permission(self, request):
        return not ProvisionerConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProvisionerFlavor)
class ProvisionerFlavorAdmin(admin.ModelAdmin):
    list_display = ("code", "label", "cores", "memory_mib", "disk_gib", "enabled")
    list_editable = ("enabled",)
    ordering = ("sort_order", "code")


@admin.register(VirtualMachine)
class VirtualMachineAdmin(admin.ModelAdmin):
    list_display = (
        "vmid",
        "hostname",
        "ipv4_address",
        "allocation",
        "flavor",
        "target_node",
        "state",
    )
    list_filter = ("state", "flavor", "target_node")
    search_fields = ("hostname", "ipv4_address", "allocation__project__title")
    readonly_fields = (
        "allocation",
        "vmid",
        "ipv4_address",
        "hostname",
        "template_vmid",
        "provisioned_at",
        "pve_deleted_at",
        "netbox_deleted_at",
        "retired_at",
        "created_at",
        "updated_at",
    )


@admin.register(IPAddressReservation)
class IPAddressReservationAdmin(admin.ModelAdmin):
    list_display = (
        "ipv4_address",
        "hostname",
        "reservation_status",
        "inventory_status",
        "vmid",
        "state",
        "allocation",
    )
    list_filter = ("state", "target_node")
    search_fields = (
        "ipv4_address",
        "hostname",
        "allocation__project__title",
        "allocation__project__pi__username",
    )
    ordering = ("vmid", "created_at")
    readonly_fields = tuple(field.name for field in VirtualMachine._meta.fields)
    actions = None

    @admin.display(description="Reservation status")
    def reservation_status(self, obj):
        return obj.ip_reservation_status

    @admin.display(description="Inventory")
    def inventory_status(self, obj):
        identifiers = (obj.netbox_vm_id, obj.netbox_interface_id, obj.netbox_ip_id)
        if all(identifiers):
            return "Built-in pool + NetBox mirror"
        if any(identifiers):
            return "NetBox mirror incomplete"
        return "Built-in pool"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProvisioningJob)
class ProvisioningJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "virtual_machine",
        "action",
        "status",
        "attempts",
        "queued_at",
        "completed_at",
    )
    list_filter = ("action", "status")
    readonly_fields = tuple(field.name for field in ProvisioningJob._meta.fields)


@admin.register(ProvisioningEvent)
class ProvisioningEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "virtual_machine", "job_label", "event_type")
    list_select_related = ("virtual_machine", "job")
    readonly_fields = tuple(field.name for field in ProvisioningEvent._meta.fields)

    @admin.display(description="Job", ordering="job__queued_at")
    def job_label(self, obj):
        if obj.job is None:
            return "—"
        return f"{obj.job.action} [{obj.job.status}]"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
