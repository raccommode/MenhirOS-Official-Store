# Menhir OS Official Store

The reviewed and signed application catalog bundled with Menhir OS. Every
container image is pinned by multi-platform OCI digest, every manifest hash is
covered by the signed `store.yaml`, and permissions are intentionally minimal.

Initial applications:

- Vaultwarden 1.36.0
- Jellyfin 10.11.11
- Nextcloud 34.0.1 with PostgreSQL and Redis

The store is pre-alpha. Do not treat a passing manifest check as a security
audit: releases additionally require image scanning, SBOM generation,
installation tests, backup tests, and a full restoration drill on AMD64 and
ARM64.

## Signing

`store.yaml.sig` is a detached base64 Ed25519 signature. The private key exists
only as a protected GitHub Actions secret; the corresponding public root is
compiled into Menhir OS. Changing an app manifest requires updating its SHA-256
in `store.yaml` and producing a new signature. The validation workflow verifies
that signature against the same immutable public key embedded in Menhir OS; a
non-empty but forged or stale signature is rejected.

The workflow also resolves every pinned OCI digest and confirms that each image
actually provides the AMD64 and ARM64 platforms declared by its application.
