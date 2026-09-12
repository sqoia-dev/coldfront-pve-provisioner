import re

from django.core.exceptions import ValidationError

SSH_PUBLIC_KEY_PATTERN = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+={0,3}(?: [^\r\n]+)?$"
)


def validate_flavor(value):
    from .models import ProvisionerFlavor

    if not ProvisionerFlavor.objects.filter(code=value, enabled=True).exists():
        supported = ", ".join(
            ProvisionerFlavor.objects.filter(enabled=True).values_list(
                "code", flat=True
            )
        )
        raise ValidationError(
            f"Choose a supported VM flavor: {supported or 'none configured'}."
        )
    return value


def validate_ssh_public_key(value):
    value = (value or "").strip()
    if "PRIVATE KEY" in value or "\n" in value or "\r" in value:
        raise ValidationError("Enter one OpenSSH public key, never a private key.")
    if not SSH_PUBLIC_KEY_PATTERN.fullmatch(value):
        raise ValidationError(
            "Enter one valid OpenSSH Ed25519, RSA, or ECDSA public key."
        )
    return value
