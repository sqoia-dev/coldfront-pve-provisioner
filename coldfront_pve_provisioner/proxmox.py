import socket
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from django.conf import settings

from .constants import retirement_backup_notes
from .models import get_configuration


class ProxmoxError(RuntimeError):
    pass


class ProxmoxClient:
    def __init__(self):
        self.configuration = get_configuration()
        self.base_url = settings.PVE_PROVISIONER_API_URL.rstrip("/")
        token_id = settings.PVE_PROVISIONER_TOKEN_ID
        token_secret = settings.PVE_PROVISIONER_TOKEN_SECRET
        if not token_id or not token_secret:
            raise ProxmoxError("The scoped Proxmox API token is not configured.")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"PVEAPIToken={token_id}={token_secret}"
        self.session.verify = getattr(settings, "PVE_PROVISIONER_VERIFY_TLS", True)
        self.timeout = (5, 30)

    def request(self, method, path, **kwargs):
        response = self.session.request(
            method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProxmoxError(
                f"Proxmox returned HTTP {response.status_code} without JSON."
            ) from exc
        if not response.ok:
            detail = body.get("errors") or body.get("message") or "request failed"
            raise ProxmoxError(f"Proxmox HTTP {response.status_code}: {detail}")
        return body.get("data")

    def get(self, path, **params):
        return self.request("GET", path, params=params or None)

    def post(self, path, **data):
        return self.request("POST", path, data=data)

    def put(self, path, **data):
        return self.request("PUT", path, data=data)

    def delete(self, path, **data):
        # PVE's API daemon rejects form bodies on DELETE with HTTP 501
        # ("Unexpected content for method 'DELETE'").  Optional delete
        # arguments such as purge must be encoded in the query string.
        return self.request("DELETE", path, params=data or None)

    def cluster_resources(self, resource_type=None):
        params = {"type": resource_type} if resource_type else {}
        return self.get("/cluster/resources", **params)

    def existing_vm(self, vmid):
        return next(
            (
                row
                for row in self.cluster_resources("vm")
                if int(row.get("vmid", -1)) == int(vmid)
            ),
            None,
        )

    def template(self, vmid):
        row = self.existing_vm(vmid)
        if not row or int(row.get("template", 0)) != 1:
            raise ProxmoxError(
                f"Required template VMID {vmid} is absent or not a template."
            )
        if row.get("name") != self.configuration.template_name:
            raise ProxmoxError(
                f"Template VMID {vmid} is {row.get('name')!r}, not the configured "
                f"{self.configuration.template_name!r}."
            )
        return row

    def choose_target_node(self, memory_mib):
        eligible = []
        for row in self.cluster_resources("node"):
            if (
                row.get("node") not in self.configuration.allowed_node_list
                or row.get("status") != "online"
            ):
                continue
            free_bytes = int(row.get("maxmem", 0)) - int(row.get("mem", 0))
            if free_bytes >= memory_mib * 1024 * 1024:
                eligible.append((free_bytes, row["node"]))
        if not eligible:
            raise ProxmoxError("No approved Proxmox node has enough free memory.")
        return max(eligible)[1]

    def wait_task(self, node, upid, timeout=600):
        deadline = time.monotonic() + timeout
        encoded = quote(upid, safe="")
        while time.monotonic() < deadline:
            status = self.get(f"/nodes/{node}/tasks/{encoded}/status")
            if status.get("status") == "stopped":
                if status.get("exitstatus") != "OK":
                    raise ProxmoxError(
                        f"Proxmox task failed: {status.get('exitstatus', 'unknown')}"
                    )
                return status
            time.sleep(3)
        raise ProxmoxError(f"Timed out waiting for Proxmox task {upid}.")

    def wait_for_guest_agent_ready(
        self,
        node,
        vmid,
        *,
        required_commands=("guest-exec", "guest-exec-status"),
        timeout=1800,
    ):
        """Wait for the guest to signal that its final QGA policy is active."""
        required = set(required_commands)
        deadline = time.monotonic() + timeout
        last_detail = "QEMU Guest Agent has not reported its command policy."
        while True:
            try:
                info = self.get(f"/nodes/{node}/qemu/{vmid}/agent/info")
            except ProxmoxError as exc:
                if "guest agent is not running" not in str(exc).lower():
                    raise
                last_detail = str(exc)
            else:
                # Proxmox wraps guest-info's payload in a result object even
                # after the ordinary API data envelope has been removed.
                if isinstance(info, dict) and isinstance(info.get("result"), dict):
                    info = info["result"]
                supported = {
                    row.get("name")
                    for row in (info or {}).get("supported_commands", [])
                    if row.get("enabled") is True
                }
                missing = sorted(required - supported)
                if not missing:
                    return info
                last_detail = f"missing or disabled commands: {', '.join(missing)}"
            if time.monotonic() >= deadline:
                raise ProxmoxError(
                    f"Timed out waiting for VMID {vmid} guest readiness ({last_detail})."
                )
            time.sleep(5)

    def guest_exec(
        self, node, vmid, command, *, input_data="", ready_timeout=45, timeout=45
    ):
        """Run one root command through the tightly scoped QEMU guest agent."""
        ready_deadline = time.monotonic() + ready_timeout
        while True:
            try:
                result = self.request(
                    "POST",
                    f"/nodes/{node}/qemu/{vmid}/agent/exec",
                    data={"command": command, "input-data": input_data},
                )
                break
            except ProxmoxError as exc:
                # Only agent startup is transient. Permission, allowlist, and
                # malformed-request failures must surface before Django-Q's
                # task timeout so the durable job cannot remain "Running".
                if "guest agent is not running" not in str(exc).lower():
                    raise
                if time.monotonic() >= ready_deadline:
                    raise
                time.sleep(5)
        pid = result.get("pid") if isinstance(result, dict) else None
        if not pid:
            raise ProxmoxError(f"VMID {vmid} guest agent returned no execution PID.")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.get(f"/nodes/{node}/qemu/{vmid}/agent/exec-status", pid=pid)
            if status.get("exited"):
                exit_code = int(status.get("exitcode", -1))
                if exit_code != 0:
                    detail = (
                        status.get("err-data")
                        or status.get("out-data")
                        or "command failed"
                    )[:4000]
                    raise ProxmoxError(
                        f"VMID {vmid} guest command exited {exit_code}: {detail}"
                    )
                return status
            time.sleep(2)
        raise ProxmoxError(
            f"Timed out waiting for VMID {vmid} guest command PID {pid}."
        )

    @staticmethod
    def wait_for_ssh(host, timeout=600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, 22), timeout=3):
                    return
            except OSError:
                time.sleep(5)
        raise ProxmoxError(
            f"VM {host} started but did not expose SSH within {timeout} seconds."
        )

    def ensure_vm(self, vm, flavor, ssh_public_key, progress=None):
        progress = progress or (lambda _event_type, _metadata=None: None)
        existing = self.existing_vm(vm.vmid)
        if existing:
            if existing.get("name") != vm.hostname:
                raise ProxmoxError(
                    f"VMID {vm.vmid} exists as {existing.get('name')!r}, not {vm.hostname!r}; refusing collision."
                )
            target = existing["node"]
        else:
            template = self.template(vm.template_vmid)
            target = self.choose_target_node(flavor.memory_mib)
            upid = self.post(
                f"/nodes/{template['node']}/qemu/{vm.template_vmid}/clone",
                newid=vm.vmid,
                name=vm.hostname,
                target=target,
                full=1,
                storage=self.configuration.storage,
                pool=self.configuration.proxmox_pool,
                description=f"Managed by ColdFront allocation {vm.allocation_id}; do not repurpose VMID or IP.",
            )
            self.wait_task(template["node"], upid)
        progress("Proxmox VM Confirmed", {"target_node": target})
        config = self.get(f"/nodes/{target}/qemu/{vm.vmid}/config")
        network = config.get("net0", "")
        if (
            f"bridge={self.configuration.bridge}" not in network
            or "firewall=1" not in network
        ):
            raise ProxmoxError(
                f"VMID {vm.vmid} did not inherit the configured "
                f"{self.configuration.bridge} firewall-enabled interface."
            )
        desired = {
            "cores": flavor.cores,
            "memory": flavor.memory_mib,
            "balloon": 0,
            "cpu": self.configuration.cpu_type,
            "agent": "enabled=1",
            "ciuser": self.configuration.guest_username,
            # PVE's sshkeys parameter is itself a URL-encoded value inside the
            # ordinary form body, so requests must encode the percent signs a
            # second time when serializing the form.
            "sshkeys": quote(ssh_public_key, safe=""),
            "ipconfig0": (
                f"ip={vm.ipv4_address}/{self.configuration.network_prefix_length},"
                f"gw={self.configuration.gateway}"
            ),
            "nameserver": " ".join(self.configuration.nameserver_list),
            "searchdomain": self.configuration.dns_search_domain,
            "onboot": 0,
            "protection": 1,
            "tags": f"coldfront;allocation-{vm.allocation_id}",
        }
        if self.configuration.cloud_init_vendor_snippet:
            desired["cicustom"] = (
                f"vendor={self.configuration.cloud_init_vendor_snippet}"
            )
        self.put(f"/nodes/{target}/qemu/{vm.vmid}/config", **desired)
        progress("Proxmox Configuration Accepted", {"target_node": target})
        current = self.get(f"/nodes/{target}/qemu/{vm.vmid}/status/current")
        if current.get("status") != "running":
            upid = self.post(f"/nodes/{target}/qemu/{vm.vmid}/status/start")
            self.wait_task(target, upid)
        progress("Proxmox VM Running", {"target_node": target})
        self.wait_for_ssh(vm.ipv4_address)
        progress("SSH Ready", {"target_node": target})
        if (
            self.configuration.guest_access_enabled
            or self.configuration.guest_policy_enabled
        ):
            self.wait_for_guest_agent_ready(target, vm.vmid)
            progress("Guest Agent Ready", {"target_node": target})
        return target

    @staticmethod
    def description(vm):
        return f"Managed by ColdFront allocation {vm.allocation_id}; do not repurpose VMID or IP."

    def require_exact_vm(self, vm):
        existing = self.existing_vm(vm.vmid)
        if not existing:
            raise ProxmoxError(f"VMID {vm.vmid} is absent.")
        if (
            existing.get("name") != vm.hostname
            or existing.get("node") != vm.target_node
        ):
            raise ProxmoxError(
                f"VMID {vm.vmid} identity or node differs from ColdFront; refusing retirement."
            )
        target = existing["node"]
        config = self.get(f"/nodes/{target}/qemu/{vm.vmid}/config")
        tags = set(filter(None, str(config.get("tags", "")).split(";")))
        expected_tags = {"coldfront", f"allocation-{vm.allocation_id}"}
        expected_ip = (
            f"ip={vm.ipv4_address}/{self.configuration.network_prefix_length},"
            f"gw={self.configuration.gateway}"
        )
        network = str(config.get("net0", ""))
        checks = (
            config.get("name") == vm.hostname,
            config.get("description") == self.description(vm),
            config.get("ipconfig0") == expected_ip,
            expected_tags.issubset(tags),
            f"bridge={self.configuration.bridge}" in network,
            "firewall=1" in network,
        )
        if not all(checks):
            raise ProxmoxError(
                f"VMID {vm.vmid} configuration differs from its exact managed identity."
            )
        return target

    def retirement_backups(self, vm):
        rows = self.get(
            f"/nodes/{vm.target_node}/storage/{self.configuration.retirement_backup_storage}/content",
            content="backup",
            vmid=vm.vmid,
        )
        expected_notes = retirement_backup_notes(self.configuration, vm.allocation_id)
        return [
            row
            for row in rows
            if row.get("volid", "").startswith(
                f"{self.configuration.retirement_backup_storage}:backup/vm/{vm.vmid}/"
            )
            and row.get("notes") == expected_notes
        ]

    def find_exact_retirement_backup(self, vm):
        rows = self.retirement_backups(vm)
        return max(rows, key=lambda row: int(row.get("ctime", 0))) if rows else None

    def start_retirement_backup(self, vm):
        target = self.require_exact_vm(vm)
        return self.post(
            f"/nodes/{target}/vzdump",
            vmid=vm.vmid,
            storage=self.configuration.retirement_backup_storage,
            mode="snapshot",
            compress="zstd",
            remove=0,
            **{
                "notes-template": retirement_backup_notes(
                    self.configuration, vm.allocation_id
                )
            },
        )

    def require_exact_retirement_backup(self, vm):
        if not vm.retirement_backup_volume.startswith(
            f"{self.configuration.retirement_backup_storage}:backup/vm/{vm.vmid}/"
        ):
            raise ProxmoxError(
                "Stored retirement backup identity is outside the ColdFront namespace."
            )
        row = next(
            (
                candidate
                for candidate in self.retirement_backups(vm)
                if candidate.get("volid") == vm.retirement_backup_volume
            ),
            None,
        )
        if row is None:
            raise ProxmoxError("The exact recorded retirement backup is absent.")
        expected_created = datetime.fromtimestamp(int(row["ctime"]), tz=timezone.utc)
        if (
            vm.retirement_backup_created_at
            and abs(
                (vm.retirement_backup_created_at - expected_created).total_seconds()
            )
            > 1
        ):
            raise ProxmoxError(
                "The recorded retirement backup timestamp differs from PBS."
            )
        return row

    def delete_exact_retirement_backup(self, vm, *, allow_absent=False):
        try:
            self.require_exact_retirement_backup(vm)
        except ProxmoxError as exc:
            if (
                allow_absent
                and str(exc) == "The exact recorded retirement backup is absent."
            ):
                return
            raise
        volume = quote(vm.retirement_backup_volume, safe="")
        self.delete(
            f"/nodes/{vm.target_node}/storage/"
            f"{self.configuration.retirement_backup_storage}/content/{volume}"
        )
        if any(
            row.get("volid") == vm.retirement_backup_volume
            for row in self.retirement_backups(vm)
        ):
            raise ProxmoxError(
                "The retirement backup remains after its deletion completed."
            )

    def delete_exact_vm(self, vm, *, allow_absent=False):
        """Retire only the exact ColdFront VM identity, never a VMID collision."""
        existing = self.existing_vm(vm.vmid)
        if not existing:
            if allow_absent:
                return vm.target_node
            raise ProxmoxError(
                f"VMID {vm.vmid} is absent without a recorded retirement intent."
            )
        target = self.require_exact_vm(vm)
        current = self.get(f"/nodes/{target}/qemu/{vm.vmid}/status/current")
        if current.get("status") == "running":
            upid = self.post(
                f"/nodes/{target}/qemu/{vm.vmid}/status/shutdown",
                timeout=120,
                forceStop=1,
            )
            self.wait_task(target, upid)
        self.put(f"/nodes/{target}/qemu/{vm.vmid}/config", protection=0)
        upid = self.delete(f"/nodes/{target}/qemu/{vm.vmid}", purge=1)
        self.wait_task(target, upid)
        if self.existing_vm(vm.vmid):
            raise ProxmoxError(
                f"VMID {vm.vmid} still exists after its retirement task completed."
            )
        return target
