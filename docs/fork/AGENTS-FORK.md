# Hermes Core Fork Agent Guide

This file contains the fork-specific rules that should stay out of the root `AGENTS.md`.

Use it for product work only:

- `hermes_cli/product_*`
- product installer/setup/runtime integration
- product tests under `tests/hermes_cli/*`
- fork docs in `docs/fork/*`

## Product Boundary

Prefer adapting the fork through sidecar product files:

- `hermes_cli/product_*`
- product-specific tests
- product config wrappers
- runtime-edge integration code

Do not modify upstream Hermes files unless an upstream-facing change is explicitly intended.

If a behavior could be fixed either:

- in upstream Hermes core, or
- in the fork-side product layer

choose the fork-side product layer.

If a change truly requires upstream Hermes core edits, call that out clearly before making it.

## Product Runtime Contract

The product runtime service should stay intentionally narrow:

- `GET /healthz`
- `GET /runtime/session`
- `POST /runtime/turn`
- `POST /runtime/turn/stream`

Rules:

- Reuse `AIAgent`; do not build a second conversation loop.
- Respect the configured Hermes runtime env and selected Hermes toolsets.
- Do not expose generic Hermes CLI, gateway, or broad admin features through the runtime service.
- Keep it focused on one turn in and one normalized response out.

## Current Product Model

Current product assumptions:

- Tailnet-only product access
- `tsidp` is the bundled auth provider
- no localhost/LAN auth surface
- first admin bootstrap uses a one-time bootstrap link
- users join through one-time invite claim links
- per-user runtime containers
- per-user Hermes installs inside runtime containers
- product workspaces are live-mounted into the user runtime
- route registration is split by concern and wired through explicit service objects
- runtime/setup/install modules should stay as thin orchestration layers over smaller helpers

Reference docs:

- `docs/fork/SPEC.md`
- `docs/fork/DEVELOPMENT.md`

## Security Defaults

Preserve these defaults unless the product decision changes:

- no LAN exposure for internal runtime control ports
- same-origin and CSRF protection for mutating browser routes
- user-scoped runtimes and workspaces
- server-side ownership of invite/bootstrap token state
- narrow browser admin scope

## Session Handover

When finishing a meaningful fork-side session, leave the repo in a state that another maintainer or agent can pick up quickly.

Minimum handover standard:

- commit only the intended change set, or clearly say what remains uncommitted
- avoid mixing temp artifacts with product code changes
- update fork docs when the product contract changed
- mention any required operator follow-up on the Linux laptop or Tailnet

Include these handover facts in the final summary when relevant:

- current branch and pushed commit
- whether `main` was updated
- whether the Linux laptop install was updated
- whether setup/config/manual operator steps are still required
- what was actually verified:
  - targeted tests
  - live laptop checks
  - model/runtime checks
- any known remaining issues or intentional follow-up work

If you touched live deployment state, also record:

- which service was restarted
- which external endpoint or URL was verified
- whether the live config was changed

## Live Environment Notes

The Linux laptop is the reference deployment target for real product checks.

When validating live behavior there:

- prefer checking the Tailnet URL, not only localhost
- verify the installed code/version if behavior looks stale
- distinguish clearly between:
  - code committed in the repo
  - code pushed to origin
  - code actually installed on the laptop

If SSH is unavailable, say so directly rather than assuming the live host was updated.

## What Not To Store Here

Do not put these in this file:

- broad upstream Hermes architecture notes already covered in root `AGENTS.md`
- transient bug notes
- one-off operator secrets or local tokens
- temporary workarounds that are not part of the product contract
