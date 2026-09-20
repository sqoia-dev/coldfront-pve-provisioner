# Changelog

## 0.1.0 - unreleased

- Generalized the original site-specific implementation for public use.
- Replaced compiled-in institutional policy with typed Django-admin configuration.
- Added administrator-managed VM flavors.
- Kept API credentials and mutation gates in protected Django settings.
- Removed the dependency on a private allocation-interface plugin.
- Made guest access synchronization and guarded retirement optional.
- Added versioned declarative guest policy for packages, managed non-secret
  files, systemd units, allocation-membership reconciliation, and bounded patch
  jobs, with a reviewed reference helper.
- Added a browser-local Ed25519 key-pair generator to the VM request page; only
  the public key is submitted through the existing cloud-init path.
- Made NetBox inventory mirroring optional; new configurations use the built-in
  IPv4 pool and database reservations by default.
- Added a read-only IP address reservations admin view and human-readable VM,
  job, and event labels.
- Added scheduled retirement-backup cleanup.
- Hardened guest maintenance so disabled policy components cannot execute queued
  work, patching and reconciliation remain single-flight per VM, and service
  reconciliation does not start inactive units when service enablement is off.
- Added packaging metadata, CI, license/notice, deployment, security,
  architecture, configuration, and portability documentation.
