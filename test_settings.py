from coldfront.config.settings import *

SECRET_KEY = "local-plugin-test-only"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
INSTALLED_APPS = [
    *INSTALLED_APPS,
    "coldfront_pve_provisioner.apps.PVEProvisionerConfig",
]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
SILENCED_SYSTEM_CHECKS = ["django_vite.W001"]
