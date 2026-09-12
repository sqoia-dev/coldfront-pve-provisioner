import requests
from django.conf import settings

from .models import get_configuration


class NetBoxError(RuntimeError):
    pass


class NetBoxClient:
    def __init__(self):
        self.configuration = get_configuration()
        self.base_url = settings.PVE_PROVISIONER_NETBOX_API_URL.rstrip("/")
        token = settings.PVE_PROVISIONER_NETBOX_TOKEN
        if not token:
            raise NetBoxError("The scoped NetBox API token is not configured.")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.session.verify = getattr(
            settings, "PVE_PROVISIONER_NETBOX_VERIFY_TLS", True
        )
        self.timeout = (5, 30)

    def request(self, method, path, *, allow_not_found=False, **kwargs):
        response = self.session.request(
            method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs
        )
        if allow_not_found and response.status_code == 404:
            return None
        if response.status_code == 204:
            return None
        try:
            body = response.json()
        except ValueError as exc:
            raise NetBoxError(
                f"NetBox returned HTTP {response.status_code} without JSON."
            ) from exc
        if not response.ok:
            raise NetBoxError(f"NetBox HTTP {response.status_code}: {body}")
        return body

    def get(self, path, **params):
        return self.request("GET", path, params=params or None)

    def get_optional(self, path):
        return self.request("GET", path, allow_not_found=True)

    def post(self, path, payload):
        return self.request("POST", path, json=payload)

    def patch(self, path, payload):
        return self.request("PATCH", path, json=payload)

    def delete(self, path):
        return self.request("DELETE", path)

    def one(self, path, **filters):
        body = self.get(path, **filters)
        results = body.get("results", [])
        if len(results) > 1:
            raise NetBoxError(f"NetBox returned multiple records for {path} {filters}.")
        return results[0] if results else None

    def ensure_named(self, path, name, payload):
        row = self.one(path, name=name)
        if row:
            return row
        return self.post(path, {"name": name, **payload})

    def ensure_foundation(self):
        cluster_type = self.ensure_named(
            "/api/virtualization/cluster-types/",
            self.configuration.netbox_cluster_type,
            {"slug": "proxmox-ve", "description": "Proxmox Virtual Environment"},
        )
        cluster = self.one(
            "/api/virtualization/clusters/", name=self.configuration.netbox_cluster_name
        )
        if cluster and cluster["type"]["id"] != cluster_type["id"]:
            raise NetBoxError(
                "The NetBox Proxmox cluster name exists with an unexpected type."
            )
        if not cluster:
            cluster = self.post(
                "/api/virtualization/clusters/",
                {
                    "name": self.configuration.netbox_cluster_name,
                    "type": cluster_type["id"],
                    "status": "active",
                },
            )
        tag = self.one("/api/extras/tags/", slug=self.configuration.netbox_managed_tag)
        if not tag:
            tag = self.post(
                "/api/extras/tags/",
                {
                    "name": "ColdFront Managed",
                    "slug": self.configuration.netbox_managed_tag,
                    "description": "Objects whose lifecycle is guarded by a ColdFront allocation",
                },
            )
        return cluster, tag

    @staticmethod
    def description(vm):
        return f"Managed by ColdFront allocation {vm.allocation_id}; VMID {vm.vmid}; do not repurpose."

    def require_managed_identity(self, row, vm, object_name):
        tag_slugs = {tag["slug"] for tag in row.get("tags", [])}
        if self.configuration.netbox_managed_tag not in tag_slugs or row.get(
            "description"
        ) != self.description(vm):
            raise NetBoxError(
                f"Existing NetBox {object_name} is not the exact ColdFront-managed identity."
            )

    def ensure_reservation(self, vm, flavor):
        cluster, tag = self.ensure_foundation()
        description = self.description(vm)
        nb_vm = self.one(
            "/api/virtualization/virtual-machines/",
            name=vm.hostname,
            cluster_id=cluster["id"],
        )
        if nb_vm:
            self.require_managed_identity(nb_vm, vm, "virtual machine")
        else:
            nb_vm = self.post(
                "/api/virtualization/virtual-machines/",
                {
                    "name": vm.hostname,
                    "cluster": cluster["id"],
                    "status": "staged",
                    "vcpus": flavor.cores,
                    "memory": flavor.memory_mib,
                    "disk": flavor.disk_gib * 1024,
                    "description": description,
                    "tags": [tag["id"]],
                },
            )
        interface = self.one(
            "/api/virtualization/interfaces/",
            virtual_machine_id=nb_vm["id"],
            name="eth0",
        )
        if not interface:
            interface = self.post(
                "/api/virtualization/interfaces/",
                {
                    "virtual_machine": nb_vm["id"],
                    "name": "eth0",
                    "enabled": True,
                    "description": description,
                    "tags": [tag["id"]],
                },
            )
        else:
            self.require_managed_identity(interface, vm, "virtual interface")
        address = f"{vm.ipv4_address}/{self.configuration.network_prefix_length}"
        ip = self.one("/api/ipam/ip-addresses/", address=address)
        if ip:
            self.require_managed_identity(ip, vm, "IP address")
            assigned = ip.get("assigned_object") or {}
            if assigned.get("id") != interface["id"]:
                raise NetBoxError(
                    f"NetBox IP {address} is assigned to a different object."
                )
        else:
            ip = self.post(
                "/api/ipam/ip-addresses/",
                {
                    "address": address,
                    "status": "reserved",
                    "dns_name": vm.hostname,
                    "assigned_object_type": "virtualization.vminterface",
                    "assigned_object_id": interface["id"],
                    "description": description,
                    "tags": [tag["id"]],
                },
            )
        self.patch(
            f"/api/virtualization/virtual-machines/{nb_vm['id']}/",
            {"primary_ip4": ip["id"]},
        )
        return {
            "vm_id": nb_vm["id"],
            "interface_id": interface["id"],
            "ip_id": ip["id"],
        }

    def activate(self, vm):
        self.patch(f"/api/ipam/ip-addresses/{vm.netbox_ip_id}/", {"status": "active"})
        self.patch(
            f"/api/virtualization/virtual-machines/{vm.netbox_vm_id}/",
            {"status": "active"},
        )

    def validate_exact_managed_records(self, vm, *, allow_absent=False):
        if not all((vm.netbox_vm_id, vm.netbox_interface_id, vm.netbox_ip_id)):
            raise NetBoxError(
                "Stored NetBox identity is incomplete; refusing deletion."
            )
        paths = (
            (f"/api/ipam/ip-addresses/{vm.netbox_ip_id}/", "IP address"),
            (
                f"/api/virtualization/interfaces/{vm.netbox_interface_id}/",
                "virtual interface",
            ),
            (
                f"/api/virtualization/virtual-machines/{vm.netbox_vm_id}/",
                "virtual machine",
            ),
        )
        rows = [(path, label, self.get_optional(path)) for path, label in paths]
        if not allow_absent and any(row is None for _path, _label, row in rows):
            raise NetBoxError(
                "A stored NetBox object is absent without a recorded retirement intent."
            )
        for _path, label, row in rows:
            if row is not None:
                self.require_managed_identity(row, vm, label)
        return rows

    def delete_exact_managed_records(self, vm, *, allow_absent=False):
        """Delete only exact, tagged records after an independently verified VM retirement."""
        rows = self.validate_exact_managed_records(vm, allow_absent=allow_absent)
        for path, _label, row in rows:
            if row is not None:
                self.delete(path)
        if any(self.get_optional(path) is not None for path, _label, _row in rows):
            raise NetBoxError("A managed NetBox object remains after retirement.")
