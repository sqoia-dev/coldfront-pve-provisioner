from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coldfront.core.resource.models import Resource
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django_q.models import Schedule

from .constants import hostname_for, ipv4_for_vmid, retirement_backup_notes
from .guest_access import normalize_usernames, render_access_reconcile_payload
from .models import ProvisionerConfiguration, ProvisionerFlavor
from .netbox import NetBoxClient, NetBoxError
from .proxmox import ProxmoxClient, ProxmoxError
from .services import validate_initial_service_term
from .tasks import _is_transient_provisioning_error
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
    def test_configuration_accepts_a_complete_generic_site(self):
        configuration = ProvisionerConfiguration(**configuration_values())
        configuration.full_clean()

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
