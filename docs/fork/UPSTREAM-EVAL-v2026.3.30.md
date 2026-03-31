# Upstream Evaluation: `v2026.3.30`

This note captures the fork-shrink evaluation done on branch
`explore/upstream-v2026.3.30-shrink`.

## Adopted

- Centralized `HERMES_HOME` handling on `hermes_constants.get_hermes_home()`.
- Added upstream-style shared helpers in `hermes_constants.py`:
  - `get_hermes_home()`
  - `get_hermes_dir()`
  - `display_hermes_home()`
  - `parse_reasoning_effort()`
- Removed a duplicated `get_hermes_home()` implementation from
  `hermes_cli.config` and re-exported the shared helper instead.
- Reduced config-coupled imports in product/shared entrypoints so they can use
  the shared path helper directly.
- Switched several user-facing messages from hardcoded `~/.hermes` text to the
  real active `HERMES_HOME` display path.

## Evaluated And Rejected

### Upstream Profiles

Rejected for the product branch in this pass.

Why:
- upstream `hermes_cli/profiles.py` is a large user-facing feature surface
  (wrapper aliases, active profile switching, export/import, gateway profile
  management);
- the product already has its own per-user isolated runtime model;
- adopting profiles now would add code and UX rather than shrinking the fork.

Decision:
- keep the product runtime/admin/bootstrap model as-is;
- only borrow shared path/home ideas, not the profile UX.

### Upstream Dockerfile

Rejected as a replacement for `Dockerfile.product`.

Why:
- upstream Dockerfile is a general full-Hermes container image;
- it installs broader shared dependencies (`.[all]`, Playwright, whatsapp
  bridge, node/npm tooling);
- it does not replace the product's per-user runtime contract or `runsc`/gVisor
  isolation model;
- `Dockerfile.product` remains materially leaner for the product runtime.

Decision:
- keep `Dockerfile.product`;
- continue using the product-specific runtime image and orchestration.

### Upstream Docker Backend As Product Runtime Replacement

Rejected in this pass.

Why:
- upstream `tools/environments/docker.py` improves shared Hermes terminal
  execution, but the product runtime is a different system;
- the product runtime still needs:
  - per-user staging,
  - runtime auth/token wiring,
  - mounted workspace isolation,
  - `runsc` integration,
  - product runtime service boot semantics.

Decision:
- keep the product runtime orchestration fork-side.

## Best Version

The leanest low-risk version from this evaluation is:

- adopt upstream-style shared path/home helpers broadly;
- keep the product runtime/auth/app surfaces fork-specific;
- do not adopt upstream profiles or the upstream Dockerfile into product UX.

This reduces duplicated shared maintenance without destabilizing the current
Tailnet-only Hermes Core product.
