# Contributing

Issues and pull requests are welcome for reproducible defects, documentation,
portability improvements, and bounded new adapters.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
PYTHONPATH=. DJANGO_SETTINGS_MODULE=test_settings django-admin test coldfront_pve_provisioner
PYTHONPATH=. DJANGO_SETTINGS_MODULE=test_settings django-admin makemigrations --check --dry-run --skip-checks
python -m build
```

Do not use real API tokens, institutional addresses, hostnames, usernames, or
customer data in tests, fixtures, issues, or pull requests. Use the RFC 5737
documentation ranges and `example` domains.

Changes that can mutate or delete infrastructure must preserve disabled-by-
default gates, exact identity checks, durable intent, retry boundaries, and a
documented recovery path.
