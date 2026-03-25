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
- Hermes-native operator CLI:
  - `hermes setup model`
  - `hermes setup tools`
  - `hermes setup gateway`
  - `hermes setup agent`
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

Current internal cleanup direction:

- `product_app.py` should act as the product HTTP composition layer:
  - shared auth/session/proxy helpers
  - route registration grouped by concern (`root`, `auth`, `chat`, `workspace`, `admin`)
- `product_runtime.py` should center on:
  - runtime launch setting resolution
  - runtime file/env staging
  - container lifecycle and health checks
- `product_install.py` should center on:
  - host prerequisite checks
  - service unit rendering
  - installer cleanup/build orchestration

Current security hardening direction:

- `product_app.py` is the server-side policy boundary for:
  - session refresh and admin checks
  - CSRF and same-origin enforcement on mutating browser routes
  - canonical-origin redirects
  - narrow Pocket ID proxying with blocked client-supplied forwarded headers
- `product_invites.py` and `product_users.py` should keep signup tokens server-side:
  - token-derived identifiers must not leak back into admin placeholder IDs
  - invite reconciliation should stay authoritative on the server
- `product_runtime.py` and `product_runtime_service.py` should treat runtime secrets/config as a narrow launch contract:
  - runtime env files must reject unsafe values such as newline-delimited secrets
  - runtime auth must stay constant-time and token-scoped
  - generated runtime config inputs remain read-only mounts

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
- `hermes-core uninstall` removes the product layer only. It does not wipe the main Hermes config unless the operator does that separately.

## Current Setup Boundary

- `hermes-core install` / `hermes-core setup` own:
  - public host / LAN exposure
  - optional Tailscale exposure
  - Pocket ID bootstrap and OIDC client wiring
  - SOUL template selection
  - per-user workspace quota
  - product service startup
- `hermes setup ...` owns:
  - model/provider configuration
  - tool policy
  - gateway/messaging setup
  - agent defaults

Product runtimes are considered ready when the Hermes config resolves to a runnable model/provider configuration. Readiness is determined from config state, not from whether a user happened to run every setup command.

## Maintainer Workflow

For any change:

1. Read relevant product-side code and tests first.
2. Implement at the product edge (`hermes_cli/product_*`) where possible.
3. Add or update focused tests.
4. Verify behavior with targeted pytest slices.
5. Update `README.md` (if user-facing) and `docs/fork/*` (if maintainer-facing).
6. Commit only intended files; avoid bundling unrelated local artifacts.

For installer/runtime changes, also smoke-test:

1. `hermes-core install` on a clean Linux or WSL environment
2. first-admin signup flow
3. `hermes setup model`
4. one real runtime turn through `/runtime/turn` or the web chat UI

## Security Defaults to Preserve

- No broad runtime filesystem access outside user workspace.
- No accidental exposure of runtime ports to LAN.
- Keep auth origin handling canonical (especially in Tailscale mode).
- Enforce same-origin plus CSRF validation for browser mutations.
- Do not trust browser-supplied forwarded headers when proxying Pocket ID.
- Do not expose signup token material through admin placeholder identifiers or logs.
- Keep admin UI narrow; avoid growing it into a full config console.
- Keep runtime launch derived from the main Hermes config rather than adding a second hidden product-side source of truth.

## Related Docs

- `README.md` (public install/use guidance)
- `docs/fork/SPEC.md` (current product contract)
- `docs/fork/UPSTREAM-SYNC.md` (sync process with upstream Hermes)
