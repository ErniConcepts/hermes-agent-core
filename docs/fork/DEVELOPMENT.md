# Hermes Core Fork Development Guide

This document describes the **current implementation state** of the `hermes-core` fork and the maintainer workflow for product-side development.

## Scope and Boundary

- Public front page: `README.md`
- Fork maintainer docs: `docs/fork/*`
- Product code boundary: prefer `hermes_cli/product_*` and product tests.
- Upstream Hermes behavior should remain unchanged unless an upstream-facing change is explicitly intended.

## Current Product Architecture

The fork adds a product layer around upstream Hermes:

- Product CLI:
  - `hermes-core install`
  - `hermes-core setup`
  - `hermes-core uninstall`
- Auth:
  - Pocket ID (bundled, Docker-managed)
  - Product app uses OIDC client flow
- Runtime:
  - Per-user runtime containers
  - gVisor (`runsc`) target path on Linux
- Web app:
  - sign-in
  - chat
  - per-user workspace upload/folder UX
  - narrow admin user management

## Current Auth and Admin Behavior

- First admin onboarding uses native Pocket ID setup bootstrap.
- `/setup` is blocked after first admin completion through the product auth proxy path.
- Admin user management is signup-token driven:
  - Admin creates signup links (no pre-created user)
  - UI shows `User` placeholders with `No signup`
  - Placeholders disappear when token is used or expired
  - Real users are listed from Pocket ID `/api/users`
- Signup mode is enforced to Pocket ID `allowUserSignups=withToken` during bootstrap and before token creation.

## Operational Expectations

- Primary production target: Linux (Ubuntu/Debian installer path).
- Windows is acceptable for development, not the deployment baseline.
- Product services are user-level where possible; host-level changes require explicit sudo.

## Maintainer Workflow

For any change:

1. Read relevant product-side code and tests first.
2. Implement at the product edge (`hermes_cli/product_*`) where possible.
3. Add or update focused tests.
4. Verify behavior with targeted pytest slices.
5. Update `README.md` (if user-facing) and `docs/fork/*` (if maintainer-facing).
6. Commit only intended files; avoid bundling unrelated local artifacts.

## Security Defaults to Preserve

- No broad runtime filesystem access outside user workspace.
- No accidental exposure of runtime ports to LAN.
- Keep auth origin handling canonical (especially in Tailscale mode).
- Keep admin UI narrow; avoid growing it into a full config console.

## Related Docs

- `README.md` (public install/use guidance)
- `docs/fork/SPEC.md` (current product contract)
- `docs/fork/UPSTREAM-SYNC.md` (sync process with upstream Hermes)
