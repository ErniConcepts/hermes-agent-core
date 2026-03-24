# Hermes Core Fork Spec (Current State)

This is the implementation-facing contract for the current `hermes-core` fork.

## Product Goal

Deliver a local, multi-user Hermes distribution that can be installed and operated with product-style workflows while keeping upstream Hermes internals largely intact.

## Primary Commands

- `hermes-core install`
  - prepares host prerequisites on supported Linux targets
  - installs product services/assets
  - runs setup unless explicitly skipped
- `hermes-core setup`
  - configures product network/auth/model/tool/workspace settings
  - bootstraps Pocket ID + OIDC client
  - starts app/runtime stack
- `hermes-core uninstall`
  - removes product-managed data/services
  - cleans up installer-managed state

## Configuration Model

- Canonical product config: `~/.hermes/product.yaml`
- Generic Hermes config remains separate (`~/.hermes/config.yaml`).
- Product config controls:
  - host/origin settings
  - Pocket ID integration
  - default model route
  - runtime toolsets
  - workspace quota

## Runtime Model

- Per-user runtime containers.
- Product runtime API surface remains narrow:
  - `GET /healthz`
  - `GET /runtime/session`
  - `POST /runtime/turn`
  - `POST /runtime/turn/stream`
- Runtime workspace is user-scoped and live-mounted for user uploads.

## Auth and Access Contract

- Pocket ID is the bundled auth provider.
- Product app is an OIDC client.
- In Tailscale mode, Tailnet URL is canonical browser/login origin.
- Native first-admin bootstrap is Pocket ID setup flow.
- Post-bootstrap, setup exposure is blocked through product auth ingress.

## Admin User Management Contract

- Admin issues signup links (token-based), not pre-created user accounts.
- Admin list is a merged view of:
  - real Pocket ID users
  - pending signup placeholders (`User`, `No signup`)
- Placeholder lifecycle:
  - appears when token is created
  - disappears when token is used or expires
  - refresh-safe due server-side reconciliation

## Security/Isolation Contract

- Runtime access remains user-scoped.
- No LAN exposure for internal runtime control ports.
- Product-side adaptation is preferred over upstream Hermes patching.
- Keep browser admin scope narrow (users/invites/deactivate), not full platform config.

## Non-Goals (Current)

- Full browser-based product configuration console.
- Broad upstream Hermes rewrites for fork-specific product concerns.
- Feature parity with every upstream surface in the product web app.
