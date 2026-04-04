# Hermes Core Fork Spec (Current State)

This is the implementation-facing contract for the current `hermes-core` fork.

## Product Goal

Deliver a Tailnet-only, multi-user Hermes distribution that can be installed and operated with product-style workflows while keeping upstream Hermes internals largely intact.

Reference architecture:

![Hermes Core Tailnet Architecture](architecture-diagram.png)

## Primary Commands

- `hermes-core install`
  - prepares host prerequisites on supported Linux targets
  - installs product services/assets
  - runs setup unless explicitly skipped
- `hermes-core setup`
  - configures product network/auth/identity/workspace settings only
  - bootstraps bundled `tsidp` + OIDC client
  - starts app/runtime stack
- `hermes-core uninstall`
  - removes product-managed data/services
  - cleans up installer-managed state

Hermes-native configuration remains on the upstream CLI surface:

- `hermes setup model`
- `hermes setup tools`
- `hermes setup agent`

## Configuration Model

- Canonical product config: `~/.hermes/product.yaml`
- Generic Hermes config remains separate (`~/.hermes/config.yaml`).
- Product config controls:
  - Tailscale/tailnet settings
  - product web branding/title
  - `tsidp` integration
  - bootstrap/invite auth state
  - workspace quota
  - runtime container infrastructure
- Hermes config controls:
  - model/provider selection
  - enabled toolsets and tools
  - gateway configuration
  - general agent behavior
- Product uninstall preserves the generic Hermes config by design.
- A reinstall therefore reuses prior model/provider settings unless the operator explicitly removes `~/.hermes/config.yaml` and related non-product env entries.

## Runtime Model

- Per-user Hermes installs running inside per-user runtime containers.
- Each user install is seeded from one operator-owned runtime template.
- Per-user runtimes resolve model/provider/tool behavior from the main Hermes config through that template.
- Per-user runtimes also inherit Hermes `session_reset` policy from the main Hermes config.
- Default runtime toolsets in this fork are `file`, `terminal`, `memory` unless the operator broadens them with normal Hermes tool configuration.
- Runtime reuse is config-aware:
  - if staged runtime env or template version differs from the running container env, the runtime container is recreated automatically
- Runtime conversation handling follows Hermes-native session behavior:
  - the full session transcript is used
  - automatic rollover is controlled by `session_reset`
  - there is no separate product-only bounded-history summary layer
- Product runtime API surface remains narrow:
  - `GET /healthz`
  - `GET /runtime/session`
  - `POST /runtime/turn`
  - `POST /runtime/turn/stream`
- Product HTTP/install/setup/runtime entry files should remain thin orchestration layers over smaller fork-side helpers.
- Runtime workspace is user-scoped and live-mounted for user uploads.
- Runtime-local `SOUL.md` and generated runtime `config.yaml` are mounted read-only inside the container.
- Each per-user Hermes home also carries a `profiles/product-runtime/` copy of the operator-owned runtime inputs so the install layout matches the upstream profile-oriented direction.
- The bundled runtime `SOUL.md` is product-specific and can be overridden by an operator-provided runtime SOUL template path in product setup.

## Auth and Access Contract

- `tsidp` is the bundled and only auth provider.
- Product app is an OIDC client.
- Tailnet URL is the only supported browser/login origin.
- First admin bootstrap uses a one-time bootstrap link created during `hermes-core setup`.
- First admin bootstrap can complete before any Hermes model is configured.
- Invited users claim accounts through one-time invite links on the Tailnet URL.
- No localhost or LAN login surface is part of the product contract.

## Admin User Management Contract

- Product users are fork-managed records keyed to Tailscale identity.
- First admin is created by the one-time bootstrap link and first successful `tsidp` login through it.
- Admin issues one-time invite links, not pre-created passwords or local accounts.
- The first Tailscale identity that opens a valid invite link and completes `tsidp` login claims that account.
- Pending invites are shown as placeholders until claimed or expired.

## Security/Isolation Contract

- Runtime access remains user-scoped.
- Per-user runtimes use a dedicated Docker bridge network, not host networking.
- Installer-managed host firewall rules should allow runtime access only to the configured host-local inference port when the model endpoint is local.
- No LAN exposure for internal runtime control ports.
- Browser-side mutations require both same-origin validation and CSRF validation.
- Read-only admin `GET` routes may omit CSRF when the response stays same-origin protected and no mutation occurs.
- `tsidp` tokens and invite/bootstrap token material must stay server-side where possible; admin placeholder IDs must not expose raw tokens.
- Product-side adaptation is preferred over upstream Hermes patching.
- Keep browser admin scope narrow (users/invites/deactivate), not full platform config.
- Current control plane is still host-installed and should be treated as an interim architecture.
- Product setup must not silently override Hermes-native model or tool configuration.
- Product sessions must use a dedicated session secret; they must never derive signing keys from OIDC client secrets.
- Public `/healthz` should stay a minimal liveness probe without issuer or topology details.

## Runtime Ownership Direction

The product runtime source of truth is operator-owned, not user-owned.

- The operator-owned product layer maintains the runtime template.
- User installs are materialized from that template and can be recreated at any time.
- The first admin runtime is not a template and must not become the source of truth for platform runtime policy.

## Non-Goals (Current)

- Full browser-based product configuration console.
- Broad upstream Hermes rewrites for fork-specific product concerns.
- Feature parity with every upstream surface in the product web app.
