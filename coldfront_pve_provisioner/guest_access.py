import re

USERNAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def normalize_usernames(usernames):
    normalized = sorted(set(usernames))
    invalid = [
        username for username in normalized if not USERNAME_PATTERN.fullmatch(username)
    ]
    if invalid:
        raise ValueError(
            "An allocation username is not safe for VM access reconciliation."
        )
    return normalized


def render_access_reconcile_payload(usernames):
    usernames = normalize_usernames(usernames)
    return "".join(f"{username}\n" for username in usernames)
