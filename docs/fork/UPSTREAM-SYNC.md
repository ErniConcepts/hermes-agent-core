# Upstream Sync Guide (Fork)

Upstream source: `https://github.com/NousResearch/hermes-agent.git`

This guide defines how to keep `hermes-core` aligned with upstream while preserving product behavior.

## Sync Policy

- Never merge upstream directly into `main` without a staging branch.
- Prefer upstream behavior by default.
- Keep product deltas concentrated in `hermes_cli/product_*` and related tests.
- If a fix can be done in fork-side adapters, do that instead of patching upstream core.

## Recommended Procedure

1. `git fetch upstream --prune`
2. `git checkout -b upstream-sync-YYYYMMDD`
3. `git merge upstream/main`
4. Resolve conflicts with minimal fork-specific changes.
5. Run focused product verification.
6. Merge back to `main` only after green checks.

## Conflict Hotspots

Commonly sensitive files:

- `pyproject.toml`
- `tests/conftest.py`
- `hermes_cli/main.py`
- `hermes_cli/product_*`

## Verification Checklist

Run at minimum:

```bash
python -m pytest tests/hermes_cli/test_product_*.py -q -o addopts=
python -m compileall hermes_cli run_agent.py
```

And smoke-check:

- `hermes-core install` flow
- `hermes-core setup` flow
- app login + chat
- admin invite creation and reconciliation

## Decision Rule for Divergence

Keep fork behavior when upstream changes would break one of these product guarantees:

- Tailnet-only multi-user product onboarding
- `tsidp`-only auth for the product web surface
- runtime isolation and user workspace boundaries
- narrow product runtime/app API surface
- clean setup boundary:
  - `hermes-core` owns product install/auth/network/storage
  - `hermes setup ...` owns model/tools/gateway/agent behavior

Document any retained divergence in commit messages and product-side tests.
