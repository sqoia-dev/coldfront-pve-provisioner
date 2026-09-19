import base64
import hashlib
import json
import re
from pathlib import PurePosixPath

from .guest_access import normalize_usernames

GUEST_POLICY_SCHEMA = "coldfront-pve-guest-policy/v1"
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:-]{0,127}$")
SERVICE_UNIT_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}\.(service|socket|timer)$"
)
ACCOUNT_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
FILE_MODE_PATTERN = re.compile(r"^0[0-7]{3}$")
TEMPLATE_PATTERN = re.compile(r"{{\s*([a-z_]+)\s*}}")
TEMPLATE_VARIABLES = {
    "allocation_id",
    "allocation_users_json",
    "allocation_users_ldap_filter",
    "allocation_users_lines",
    "hostname",
    "ipv4_address",
    "vmid",
}
ALLOWED_FILE_ROOTS = (
    PurePosixPath("/etc"),
    PurePosixPath("/opt"),
    PurePosixPath("/usr/local"),
)
DENIED_FILE_PATHS = {
    PurePosixPath("/etc/group"),
    PurePosixPath("/etc/gshadow"),
    PurePosixPath("/etc/passwd"),
    PurePosixPath("/etc/shadow"),
    PurePosixPath("/etc/sudoers"),
}
MAX_MANAGED_FILE_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
PRIVATE_MATERIAL_MARKERS = (
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
)


def normalize_packages(value):
    packages = tuple(
        sorted({line.strip() for line in value.splitlines() if line.strip()})
    )
    invalid = [
        package for package in packages if not PACKAGE_NAME_PATTERN.fullmatch(package)
    ]
    if invalid:
        raise ValueError(f"Invalid package name: {invalid[0]}")
    return packages


def normalize_service_units(value):
    units = tuple(sorted({line.strip() for line in value.splitlines() if line.strip()}))
    invalid = [unit for unit in units if not SERVICE_UNIT_PATTERN.fullmatch(unit)]
    if invalid:
        raise ValueError(f"Invalid systemd unit: {invalid[0]}")
    return units


def validate_managed_file_path(value):
    if not value or any(ord(character) < 32 for character in value):
        raise ValueError(
            "Managed file paths may not be empty or contain control characters."
        )
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(
            "Managed file paths must be absolute and may not contain '..'."
        )
    if path in DENIED_FILE_PATHS or str(path).startswith("/etc/sudoers.d/"):
        raise ValueError(
            "This account or privilege file cannot be managed by guest policy."
        )
    if path in ALLOWED_FILE_ROOTS:
        raise ValueError("Managed file paths must name a file beneath an allowed root.")
    if not any(path.is_relative_to(root) for root in ALLOWED_FILE_ROOTS):
        raise ValueError("Managed files must be beneath /etc, /opt, or /usr/local.")
    return str(path)


def validate_managed_file_content(value):
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_MANAGED_FILE_BYTES:
        raise ValueError("Managed file templates may not exceed 64 KiB.")
    if any(marker in value for marker in PRIVATE_MATERIAL_MARKERS):
        raise ValueError("Private key material may not be stored in guest policy.")
    unsupported = sorted(set(TEMPLATE_PATTERN.findall(value)) - TEMPLATE_VARIABLES)
    if unsupported:
        raise ValueError(f"Unsupported guest file template variable: {unsupported[0]}")
    return value


def validate_file_identity(owner, group, mode):
    if not ACCOUNT_NAME_PATTERN.fullmatch(owner):
        raise ValueError("Managed file owner must be a safe local account name.")
    if not ACCOUNT_NAME_PATTERN.fullmatch(group):
        raise ValueError("Managed file group must be a safe local group name.")
    if not FILE_MODE_PATTERN.fullmatch(mode):
        raise ValueError("Managed file mode must be four octal digits, such as 0644.")
    parsed_mode = int(mode, 8)
    if parsed_mode & 0o111 or parsed_mode & 0o002:
        raise ValueError(
            "Managed configuration files may not be executable or world-writable."
        )


def ldap_access_filter(usernames):
    usernames = normalize_usernames(usernames)
    if not usernames:
        return "(uid=__coldfront_no_selected_user__)"
    return "(|" + "".join(f"(uid={username})" for username in usernames) + ")"


def render_file_template(template, vm, usernames):
    usernames = normalize_usernames(usernames)
    replacements = {
        "allocation_id": str(vm.allocation_id),
        "allocation_users_json": json.dumps(usernames, separators=(",", ":")),
        "allocation_users_ldap_filter": ldap_access_filter(usernames),
        "allocation_users_lines": "\n".join(usernames),
        "hostname": vm.hostname,
        "ipv4_address": vm.ipv4_address,
        "vmid": str(vm.vmid),
    }

    def replace(match):
        name = match.group(1)
        if name not in replacements:
            raise ValueError(f"Unsupported guest file template variable: {name}")
        return replacements[name]

    return validate_managed_file_content(TEMPLATE_PATTERN.sub(replace, template))


def render_guest_policy(configuration, vm, usernames, *, apply_updates=False):
    usernames = normalize_usernames(usernames)
    files = []
    for managed_file in configuration.guest_managed_files.filter(enabled=True).order_by(
        "sort_order", "path"
    ):
        content = render_file_template(managed_file.content_template, vm, usernames)
        files.append(
            {
                "content_base64": base64.b64encode(content.encode("utf-8")).decode(
                    "ascii"
                ),
                "group": managed_file.group,
                "mode": managed_file.mode,
                "owner": managed_file.owner,
                "path": managed_file.path,
            }
        )
    manifest = {
        "allocation": {
            "hostname": vm.hostname,
            "id": vm.allocation_id,
            "ipv4_address": vm.ipv4_address,
            "users": usernames,
            "vmid": vm.vmid,
        },
        "files": files,
        "packages": {
            "manager": configuration.guest_package_manager,
            "names": list(configuration.guest_package_list),
            "patch_mode": configuration.guest_patch_mode if apply_updates else "none",
        },
        "schema": GUEST_POLICY_SCHEMA,
        "services": {
            "enable": configuration.guest_enable_services,
            "units": list(configuration.guest_service_unit_list),
        },
    }
    payload = json.dumps(manifest, separators=(",", ":"), sort_keys=True)
    if len(payload.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ValueError("Rendered guest policy may not exceed 256 KiB.")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return payload, digest
