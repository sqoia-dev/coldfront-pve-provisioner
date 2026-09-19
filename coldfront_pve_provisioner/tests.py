import base64
import json
import runpy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coldfront.core.allocation.models import (
    Allocation,
    AllocationStatusChoice,
    AllocationUser,
    AllocationUserStatusChoice,
)
from coldfront.core.field_of_science.models import FieldOfScience
from coldfront.core.project.models import Project, ProjectStatusChoice
from coldfront.core.resource.models import Resource
from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase, override_settings
from django_q.models import Schedule

from .constants import (
    GUEST_PATCH_SCHEDULE_NAME,
    hostname_for,
    ipv4_for_vmid,
    retirement_backup_notes,
)
from .forms import PVEAllocationRequestForm
from .guest_access import normalize_usernames, render_access_reconcile_payload
from .guest_policy import (
    ldap_access_filter,
    normalize_packages,
    normalize_service_units,
    render_guest_policy,
    validate_managed_file_path,
)
from .models import (
    GuestManagedFile,
    IPAddressReservation,
    ProvisionerConfiguration,
    ProvisionerFlavor,
    ProvisioningEvent,
    ProvisioningJob,
    VirtualMachine,
)
from .netbox import NetBoxClient, NetBoxError
from .proxmox import ProxmoxClient, ProxmoxError
from .services import (
    create_access_reconciliation_job,
    create_guest_patch_job,
    validate_initial_service_term,
)
from .tasks import (
    _apply_guest_policy,
    _is_transient_provisioning_error,
    _optional_netbox,
    queue_due_guest_patch_jobs,
)
from .validators import validate_ssh_public_key
from .views import operational_status_payload


def configuration_values(**overrides):
    values = {
        "enabled": True,
        "resource_name": "Research VM",
        "resource_description": "Test VMs",
        "service_term_months": 3,
        "vmid_min": 2000,
        "vmid_max": 2002,
        "ipv4_pool_start": "192.0.2.40",
        "ipv4_pool_end": "192.0.2.42",
        "network_prefix_length": 24,
        "gateway": "192.0.2.1",
        "nameservers": "192.0.2.2\n192.0.2.3",
        "dns_search_domain": "research.example",
        "hostname_template": "cf-a{allocation_id}-v{vmid}.{domain}",
        "template_vmid": 9000,
        "template_name": "linux-cloud-template",
        "storage": "shared-vm",
        "bridge": "vmbr10",
        "cpu_type": "host",
        "allowed_nodes": "pve01\npve02",
        "proxmox_pool": "coldfront-managed",
        "cloud_init_vendor_snippet": "shared-vm:snippets/site.yml",
        "netbox_enabled": True,
        "netbox_cluster_name": "research-pve",
        "netbox_managed_tag": "coldfront-managed",
        "guest_access_enabled": True,
        "guest_username": "cloud-user",
        "guest_access_helper": "/usr/local/libexec/reconcile-access",
        "retirement_enabled": True,
        "retirement_backup_storage": "retirement-pbs",
        "retirement_backup_retention_days": 14,
    }
    values.update(overrides)
    return values


class PurePolicyTests(SimpleTestCase):
    def setUp(self):
        self.configuration = SimpleNamespace(**configuration_values())

    def test_pool_boundaries_map_deterministically(self):
        self.assertEqual(ipv4_for_vmid(self.configuration, 2000), "192.0.2.40")
        self.assertEqual(ipv4_for_vmid(self.configuration, 2002), "192.0.2.42")

    def test_out_of_pool_vmid_is_rejected(self):
        with self.assertRaises(ValueError):
            ipv4_for_vmid(self.configuration, 1999)

    def test_hostname_template_is_site_configurable(self):
        self.assertEqual(
            hostname_for(self.configuration, 51, 2000),
            "cf-a51-v2000.research.example",
        )

    def test_backup_notes_use_configured_retention(self):
        self.assertEqual(
            retirement_backup_notes(self.configuration, 51),
            "ColdFront allocation 51 retirement recovery; retain 14 days",
        )

    def test_public_key_validator_rejects_private_material(self):
        with self.assertRaises(ValidationError):
            validate_ssh_public_key("-----BEGIN OPENSSH PRIVATE KEY-----")

    def test_public_key_validator_accepts_ed25519(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEU1VzQzRk1La2hTdG9ESmVjUDN0a0NBNUY1cTBvWm5QbEw test"
        self.assertEqual(validate_ssh_public_key(key), key)

    def test_access_payload_normalizes_and_rejects_unsafe_names(self):
        self.assertEqual(
            render_access_reconcile_payload(["user2", "user1"]), "user1\nuser2\n"
        )
        with self.assertRaises(ValueError):
            normalize_usernames(["user)(uid=*)"])

    def test_guest_declarations_are_normalized_and_reject_commands(self):
        self.assertEqual(
            normalize_packages("sssd\nqemu-guest-agent\nsssd\n"),
            ("qemu-guest-agent", "sssd"),
        )
        self.assertEqual(
            normalize_service_units("sssd.service\noddjobd.socket\n"),
            ("oddjobd.socket", "sssd.service"),
        )
        with self.assertRaises(ValueError):
            normalize_packages("/tmp/site-package.rpm")
        with self.assertRaises(ValueError):
            normalize_service_units("systemctl restart sssd")

    def test_guest_file_and_ldap_policy_fail_closed(self):
        self.assertEqual(ldap_access_filter([]), "(uid=__coldfront_no_selected_user__)")
        self.assertEqual(
            ldap_access_filter(["bob", "alice"]),
            "(|(uid=alice)(uid=bob))",
        )
        for path in ("/etc", "/etc/passwd", "/etc/sudoers.d/site", "/tmp/site.conf"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_managed_file_path(path)

    def test_retry_policy_is_bounded(self):
        self.assertTrue(
            _is_transient_provisioning_error("Proxmox HTTP 503: unavailable")
        )
        self.assertFalse(_is_transient_provisioning_error("Proxmox HTTP 403: denied"))

    def test_operational_status_separates_ready_from_approval(self):
        self.assertEqual(
            operational_status_payload("Active", "Active")["label"], "Ready"
        )


class ConfigurationModelTests(TestCase):
    def test_new_configuration_uses_internal_ipam_by_default(self):
        configuration = ProvisionerConfiguration(allowed_nodes="pve01")
        self.assertFalse(configuration.netbox_enabled)
        configuration.full_clean()

    def test_configuration_accepts_a_complete_generic_site(self):
        configuration = ProvisionerConfiguration(**configuration_values())
        configuration.full_clean()

    def test_declarative_policy_can_use_a_baked_image_without_cloud_init(self):
        configuration = ProvisionerConfiguration(
            **configuration_values(
                cloud_init_vendor_snippet="",
                guest_access_enabled=False,
                guest_policy_enabled=True,
                guest_package_manager="dnf",
                guest_packages="sssd\noddjob",
                guest_service_units="sssd.service\noddjobd.service",
                guest_patch_mode="security",
                guest_patch_interval_days=7,
            )
        )
        configuration.full_clean()

    def test_declarative_policy_rejects_nonportable_apt_security_mode(self):
        configuration = ProvisionerConfiguration(
            **configuration_values(
                guest_access_enabled=False,
                guest_policy_enabled=True,
                guest_package_manager="apt",
                guest_patch_mode="security",
            )
        )
        with self.assertRaisesMessage(ValidationError, "Security-only patching"):
            configuration.full_clean()

    def test_managed_file_validates_path_content_and_identity(self):
        configuration = ProvisionerConfiguration.objects.create(
            **configuration_values(guest_access_enabled=False)
        )
        managed = GuestManagedFile(
            configuration=configuration,
            path="/etc/sssd/conf.d/coldfront.conf",
            content_template="ldap_access_filter = {{ allocation_users_ldap_filter }}\n",
            mode="0600",
        )
        managed.full_clean()
        for path in ("/etc/shadow", "/var/lib/site.conf"):
            managed.path = path
            with self.subTest(path=path), self.assertRaises(ValidationError):
                managed.full_clean()
        managed.path = "/etc/site.conf"
        managed.content_template = "{{ unsupported_value }}"
        with self.assertRaisesMessage(
            ValidationError, "Unsupported guest file template"
        ):
            managed.full_clean()

    def test_guest_policy_manifest_is_allocation_aware_and_stable(self):
        configuration = ProvisionerConfiguration.objects.create(
            **configuration_values(
                guest_access_enabled=False,
                guest_policy_enabled=True,
                guest_package_manager="dnf",
                guest_packages="sssd\nqemu-guest-agent",
                guest_service_units="sssd.service",
                guest_patch_mode="security",
            )
        )
        GuestManagedFile.objects.create(
            configuration=configuration,
            path="/etc/sssd/conf.d/coldfront.conf",
            content_template=(
                "host = {{ hostname }}\n"
                "ldap_access_filter = {{ allocation_users_ldap_filter }}\n"
            ),
            mode="0600",
        )
        vm = SimpleNamespace(
            allocation_id=51,
            vmid=2000,
            hostname="cf-a51-v2000.research.example",
            ipv4_address="192.0.2.40",
        )
        payload, digest = render_guest_policy(
            configuration, vm, ["bob", "alice"], apply_updates=False
        )
        manifest = json.loads(payload)
        content = base64.b64decode(manifest["files"][0]["content_base64"]).decode()
        self.assertEqual(manifest["schema"], "coldfront-pve-guest-policy/v1")
        self.assertEqual(manifest["allocation"]["users"], ["alice", "bob"])
        self.assertEqual(manifest["packages"]["names"], ["qemu-guest-agent", "sssd"])
        self.assertEqual(manifest["packages"]["patch_mode"], "none")
        self.assertIn("ldap_access_filter = (|(uid=alice)(uid=bob))", content)
        self.assertEqual(len(digest), 64)

    def test_configuration_rejects_pool_smaller_than_vmid_range(self):
        configuration = ProvisionerConfiguration(
            **configuration_values(ipv4_pool_end="192.0.2.41")
        )
        with self.assertRaises(ValidationError):
            configuration.full_clean()

    def test_configuration_requires_backup_storage_for_retirement(self):
        configuration = ProvisionerConfiguration(
            **configuration_values(retirement_backup_storage="")
        )
        with self.assertRaises(ValidationError):
            configuration.full_clean()

    def test_configuration_rejects_gateway_inside_allocation_pool(self):
        configuration = ProvisionerConfiguration(
            **configuration_values(gateway="192.0.2.41")
        )
        with self.assertRaisesMessage(
            ValidationError, "gateway must not be inside the allocation pool"
        ):
            configuration.full_clean()

    def test_flavors_are_admin_managed(self):
        flavor = ProvisionerFlavor.objects.create(
            code="small", label="Small", cores=2, memory_mib=4096, disk_gib=40
        )
        self.assertEqual(str(flavor), "Small")

    def test_service_term_uses_admin_policy(self):
        ProvisionerConfiguration.objects.create(**configuration_values())
        valid = SimpleNamespace(
            start_date=date(2026, 1, 15), end_date=date(2026, 4, 15)
        )
        invalid = SimpleNamespace(
            start_date=date(2026, 1, 15), end_date=date(2026, 7, 15)
        )
        self.assertIsNone(validate_initial_service_term(valid))
        with self.assertRaisesMessage(RuntimeError, "exactly 3 calendar months"):
            validate_initial_service_term(invalid)

    def test_catalog_sync_uses_admin_policy_and_creates_cleanup_schedule(self):
        ProvisionerConfiguration.objects.create(**configuration_values())
        ProvisionerFlavor.objects.create(
            code="small", label="Small", cores=2, memory_mib=4096, disk_gib=40
        )
        call_command("configure_pve_provisioner", "--apply", verbosity=0)
        call_command("configure_pve_provisioner", "--check", verbosity=0)
        resource = Resource.objects.get(name="Research VM")
        self.assertEqual(resource.description, "Test VMs")
        self.assertEqual(
            resource.get_attribute("Service Term", typed=False),
            "3 months; renewal requires staff review",
        )
        self.assertTrue(
            Schedule.objects.filter(
                name="PVE VM Provisioner retirement-backup cleanup",
                func="coldfront_pve_provisioner.tasks.cleanup_expired_retirement_backups",
            ).exists()
        )

    def test_catalog_sync_configures_the_optional_patch_queue(self):
        ProvisionerConfiguration.objects.create(
            **configuration_values(
                guest_access_enabled=False,
                guest_policy_enabled=True,
                guest_package_manager="dnf",
                guest_patch_mode="security",
                guest_patch_interval_days=7,
            )
        )
        ProvisionerFlavor.objects.create(
            code="small", label="Small", cores=2, memory_mib=4096, disk_gib=40
        )
        call_command("configure_pve_provisioner", "--apply", verbosity=0)
        self.assertTrue(
            Schedule.objects.filter(
                name=GUEST_PATCH_SCHEDULE_NAME,
                func="coldfront_pve_provisioner.tasks.queue_due_guest_patch_jobs",
                schedule_type=Schedule.DAILY,
            ).exists()
        )


class GuestPolicyJobTests(TestCase):
    def setUp(self):
        self.configuration = ProvisionerConfiguration.objects.create(
            **configuration_values(
                guest_access_enabled=False,
                guest_policy_enabled=True,
                guest_package_manager="dnf",
                guest_packages="sssd",
                guest_service_units="sssd.service",
                guest_patch_mode="security",
                guest_patch_interval_days=7,
            )
        )
        GuestManagedFile.objects.create(
            configuration=self.configuration,
            path="/etc/sssd/conf.d/coldfront.conf",
            content_template="ldap_access_filter = {{ allocation_users_ldap_filter }}\n",
            mode="0600",
        )
        user = User.objects.create_user(username="alice")
        project_status, _ = ProjectStatusChoice.objects.get_or_create(name="Active")
        field = FieldOfScience.objects.create(description="Guest policy test field")
        project = Project.objects.create(
            title="Guest policy test project",
            pi=user,
            field_of_science=field,
            status=project_status,
        )
        status = AllocationStatusChoice.objects.filter(name="Active").first()
        if status is None:
            status = AllocationStatusChoice.objects.create(name="Active")
        self.allocation = Allocation.objects.create(
            project=project,
            status=status,
            justification="Test guest policy reconciliation.",
        )
        user_status = AllocationUserStatusChoice.objects.filter(name="Active").first()
        if user_status is None:
            user_status = AllocationUserStatusChoice.objects.create(name="Active")
        AllocationUser.objects.create(
            allocation=self.allocation, user=user, status=user_status
        )
        self.vm = VirtualMachine.objects.create(
            allocation=self.allocation,
            vmid=2000,
            ipv4_address="192.0.2.40",
            hostname=f"cf-a{self.allocation.pk}-v2000.research.example",
            flavor="small",
            template_vmid=9000,
            target_node="pve01",
            state=VirtualMachine.State.ACTIVE,
        )

    def test_membership_change_queues_a_policy_reconciliation(self):
        job = create_access_reconciliation_job(
            self.allocation.pk, membership_change=True
        )
        self.assertEqual(job.action, ProvisioningJob.Action.RECONCILE)
        self.assertEqual(
            job.metadata, {"directory_access": False, "guest_policy": True}
        )
        self.vm.refresh_from_db()
        self.assertEqual(self.vm.access_desired_users, ["alice"])

    def test_membership_trigger_can_be_disabled_without_disabling_manual_sync(self):
        self.configuration.guest_reconcile_on_membership_change = False
        self.configuration.save()
        self.assertIsNone(
            create_access_reconciliation_job(self.allocation.pk, membership_change=True)
        )
        self.assertIsNotNone(create_access_reconciliation_job(self.allocation.pk))

    def test_patch_job_is_single_flight(self):
        job = create_guest_patch_job(self.allocation.pk)
        self.assertEqual(job.action, ProvisioningJob.Action.PATCH)
        self.assertEqual(create_guest_patch_job(self.allocation.pk), job)

    @patch("coldfront_pve_provisioner.tasks.dispatch_job")
    def test_due_patch_queue_dispatches_the_created_job(self, dispatch):
        result = queue_due_guest_patch_jobs()
        self.assertEqual(result, {"status": "succeeded", "queued": 1})
        job = ProvisioningJob.objects.get(action=ProvisioningJob.Action.PATCH)
        dispatch.assert_called_once_with(job)

    def test_patch_records_the_steady_policy_hash(self):
        job = ProvisioningJob.objects.create(
            virtual_machine=self.vm,
            action=ProvisioningJob.Action.PATCH,
            status=ProvisioningJob.Status.RUNNING,
        )
        pve = Mock()
        _apply_guest_policy(
            pve,
            "pve01",
            self.vm,
            job,
            ["alice"],
            apply_updates=True,
        )
        applied_manifest = json.loads(pve.guest_exec.call_args.kwargs["input_data"])
        _, baseline_hash = render_guest_policy(
            self.configuration, self.vm, ["alice"], apply_updates=False
        )
        self.vm.refresh_from_db()
        self.assertEqual(applied_manifest["packages"]["patch_mode"], "security")
        self.assertEqual(self.vm.guest_policy_hash, baseline_hash)
        self.assertIsNotNone(self.vm.guest_patched_at)


class FrontendKeyGenerationTests(SimpleTestCase):
    def test_request_template_loads_the_local_key_generator(self):
        template = get_template("coldfront_pve_provisioner/allocation_create.html")
        self.assertIn("ssh_key_generator.js", template.template.source)
        self.assertIn(
            "Only the public key is submitted",
            PVEAllocationRequestForm.base_fields["vm_ssh_public_key"].help_text,
        )

    def test_key_generator_has_no_network_or_private_key_submission_path(self):
        script_path = finders.find("coldfront_pve_provisioner/ssh_key_generator.js")
        self.assertIsNotNone(script_path)
        script = Path(script_path).read_text()
        self.assertIn('generateKey(\n          { name: "Ed25519" }', script)
        self.assertIn("BEGIN OPENSSH PRIVATE KEY", script)
        self.assertNotIn("fetch(", script)
        self.assertNotIn("XMLHttpRequest", script)


class ReferenceGuestHelperTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        helper_path = (
            Path(__file__).resolve().parent.parent
            / "examples"
            / "coldfront_guest_reconcile.py.example"
        )
        cls.helper = runpy.run_path(helper_path)

    def test_reference_helper_rejects_unsafe_package_and_path(self):
        manifest = {
            "allocation": {
                "hostname": "vm.example",
                "id": 51,
                "ipv4_address": "192.0.2.40",
                "users": ["alice"],
                "vmid": 2000,
            },
            "files": [],
            "packages": {
                "manager": "dnf",
                "names": ["/tmp/site.rpm"],
                "patch_mode": "none",
            },
            "schema": "coldfront-pve-guest-policy/v1",
            "services": {"enable": True, "units": []},
        }
        with self.assertRaises(self.helper["PolicyError"]):
            self.helper["validate_manifest"](manifest)
        manifest["packages"]["names"] = []
        manifest["files"] = [
            {
                "content_base64": base64.b64encode(b"unsafe").decode(),
                "group": "root",
                "mode": "0644",
                "owner": "root",
                "path": "/etc/passwd",
            }
        ]
        with self.assertRaises(self.helper["PolicyError"]):
            self.helper["validate_manifest"](manifest)


class AdminPresentationTests(SimpleTestCase):
    def setUp(self):
        self.vm = VirtualMachine(
            hostname="cf-a51-v2000.research.example",
            vmid=2000,
            ipv4_address="192.0.2.40",
        )

    def test_operational_models_have_human_readable_labels(self):
        self.assertEqual(
            str(self.vm),
            "cf-a51-v2000.research.example (VMID 2000, 192.0.2.40)",
        )
        job = ProvisioningJob(
            virtual_machine=self.vm,
            action=ProvisioningJob.Action.PROVISION,
            status=ProvisioningJob.Status.RUNNING,
        )
        self.assertEqual(
            str(job),
            "Provision cf-a51-v2000.research.example (VMID 2000, 192.0.2.40) [Running]",
        )
        event = ProvisioningEvent(
            virtual_machine=self.vm, event_type="Internal IP Reservation Confirmed"
        )
        self.assertEqual(
            str(event),
            "Internal IP Reservation Confirmed — "
            "cf-a51-v2000.research.example (VMID 2000, 192.0.2.40)",
        )

    def test_ip_reservations_have_a_dedicated_read_only_admin_view(self):
        self.assertTrue(IPAddressReservation._meta.proxy)
        model_admin = admin.site._registry[IPAddressReservation]
        self.assertFalse(model_admin.has_add_permission(Mock()))
        self.assertFalse(model_admin.has_change_permission(Mock()))
        self.assertFalse(model_admin.has_delete_permission(Mock()))

    def test_ip_reservation_status_distinguishes_recovery_and_release(self):
        self.assertEqual(self.vm.ip_reservation_status, "Reserved")
        self.vm.state = VirtualMachine.State.RETIRED
        self.assertEqual(self.vm.ip_reservation_status, "Held for recovery")
        self.vm.retirement_backup_deleted_at = object()
        self.assertEqual(self.vm.ip_reservation_status, "Released")

    @patch("coldfront_pve_provisioner.tasks.NetBoxClient")
    def test_disabled_netbox_does_not_construct_a_client(self, client_class):
        configuration = SimpleNamespace(netbox_enabled=False)
        self.assertIsNone(_optional_netbox(configuration))
        client_class.assert_not_called()


@override_settings(
    PVE_PROVISIONER_API_URL="https://pve.example:8006/api2/json",
    PVE_PROVISIONER_TOKEN_ID="coldfront@pve!worker",
    PVE_PROVISIONER_TOKEN_SECRET="not-a-real-secret",
    PVE_PROVISIONER_VERIFY_TLS=True,
)
class ProxmoxClientTests(TestCase):
    def setUp(self):
        self.configuration = ProvisionerConfiguration.objects.create(
            **configuration_values()
        )
        self.flavor = ProvisionerFlavor.objects.create(
            code="small", label="Small", cores=2, memory_mib=4096, disk_gib=40
        )

    @patch("coldfront_pve_provisioner.proxmox.requests.Session")
    def test_delete_encodes_arguments_in_query_string(self, session_class):
        response = Mock(ok=True)
        response.json.return_value = {"data": "delete-upid"}
        session_class.return_value.request.return_value = response
        client = ProxmoxClient()
        self.assertEqual(
            client.delete("/nodes/pve01/qemu/2000", purge=1), "delete-upid"
        )
        session_class.return_value.request.assert_called_once_with(
            "DELETE",
            "https://pve.example:8006/api2/json/nodes/pve01/qemu/2000",
            timeout=(5, 30),
            params={"purge": 1},
        )

    @patch("coldfront_pve_provisioner.proxmox.requests.Session")
    def test_collision_is_refused(self, _session_class):
        client = ProxmoxClient()
        client.cluster_resources = Mock(
            return_value=[{"vmid": 2000, "name": "unrelated", "node": "pve01"}]
        )
        vm = SimpleNamespace(vmid=2000, hostname="cf-a51-v2000.research.example")
        with self.assertRaisesMessage(ProxmoxError, "refusing collision"):
            client.ensure_vm(vm, self.flavor, "ssh-ed25519 AAAA test")

    @patch("coldfront_pve_provisioner.proxmox.requests.Session")
    def test_vm_configuration_uses_admin_policy(self, _session_class):
        client = ProxmoxClient()
        client.existing_vm = Mock(
            return_value={
                "vmid": 2000,
                "name": "cf-a51-v2000.research.example",
                "node": "pve01",
            }
        )
        client.get = Mock(
            side_effect=[
                {"net0": "virtio=00:11:22:33:44:55,bridge=vmbr10,firewall=1"},
                {"status": "running"},
            ]
        )
        client.put = Mock()
        client.wait_for_ssh = Mock()
        client.wait_for_guest_agent_ready = Mock()
        progress = Mock()
        vm = SimpleNamespace(
            vmid=2000,
            hostname="cf-a51-v2000.research.example",
            ipv4_address="192.0.2.40",
            allocation_id=51,
        )
        client.ensure_vm(
            vm, self.flavor, "ssh-ed25519 AAAA+key= test", progress=progress
        )
        desired = client.put.call_args.kwargs
        self.assertEqual(desired["ciuser"], "cloud-user")
        self.assertEqual(desired["ipconfig0"], "ip=192.0.2.40/24,gw=192.0.2.1")
        self.assertEqual(desired["cicustom"], "vendor=shared-vm:snippets/site.yml")
        client.wait_for_guest_agent_ready.assert_called_once_with("pve01", 2000)

    @patch("coldfront_pve_provisioner.proxmox.requests.Session")
    def test_retirement_backup_is_scoped_by_configuration(self, _session_class):
        client = ProxmoxClient()
        client.require_exact_vm = Mock(return_value="pve01")
        client.post = Mock(return_value="backup-upid")
        vm = SimpleNamespace(allocation_id=51, vmid=2000, target_node="pve01")
        self.assertEqual(client.start_retirement_backup(vm), "backup-upid")
        client.post.assert_called_once_with(
            "/nodes/pve01/vzdump",
            vmid=2000,
            storage="retirement-pbs",
            mode="snapshot",
            compress="zstd",
            remove=0,
            **{
                "notes-template": "ColdFront allocation 51 retirement recovery; retain 14 days"
            },
        )


@override_settings(
    PVE_PROVISIONER_NETBOX_API_URL="https://netbox.example",
    PVE_PROVISIONER_NETBOX_TOKEN="not-a-real-token",
    PVE_PROVISIONER_NETBOX_VERIFY_TLS=True,
)
class NetBoxClientTests(TestCase):
    def setUp(self):
        ProvisionerConfiguration.objects.create(**configuration_values())

    @patch("coldfront_pve_provisioner.netbox.requests.Session")
    def test_unmanaged_collision_is_refused(self, _session_class):
        client = NetBoxClient()
        vm = SimpleNamespace(allocation_id=51, vmid=2000)
        with self.assertRaisesMessage(
            NetBoxError, "not the exact ColdFront-managed identity"
        ):
            client.require_managed_identity(
                {"description": "Unrelated record", "tags": []}, vm, "IP address"
            )

    @patch("coldfront_pve_provisioner.netbox.requests.Session")
    def test_exact_records_are_validated_before_ordered_deletion(self, _session_class):
        client = NetBoxClient()
        vm = SimpleNamespace(
            allocation_id=51,
            vmid=2000,
            netbox_vm_id=10,
            netbox_interface_id=20,
            netbox_ip_id=30,
        )
        managed = {
            "description": client.description(vm),
            "tags": [{"slug": "coldfront-managed"}],
        }
        client.get_optional = Mock(
            side_effect=[managed, managed, managed, None, None, None]
        )
        client.delete = Mock()
        client.delete_exact_managed_records(vm)
        self.assertEqual(
            [item.args[0] for item in client.delete.call_args_list],
            [
                "/api/ipam/ip-addresses/30/",
                "/api/virtualization/interfaces/20/",
                "/api/virtualization/virtual-machines/10/",
            ],
        )


class DisabledNetBoxClientTests(TestCase):
    def test_client_refuses_use_when_admin_integration_is_disabled(self):
        ProvisionerConfiguration.objects.create(
            **configuration_values(netbox_enabled=False)
        )
        with self.assertRaisesMessage(NetBoxError, "disabled in Django admin"):
            NetBoxClient()
