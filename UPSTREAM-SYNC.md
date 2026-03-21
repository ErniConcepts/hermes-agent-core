# Upstream Sync

This repo is a fork of:

- `https://github.com/NousResearch/hermes-agent.git`

The fork should stay as close to upstream as possible. Product-specific behavior should live at the runtime edge, not deep inside Hermes core.

## Current remotes

- `origin` -> `git@github.com:ErniConcepts/hermes-agent-core.git`
- `upstream` -> `https://github.com/NousResearch/hermes-agent.git`

## Sync policy

- Do not merge upstream directly into `main` blindly.
- Always sync in a temporary branch first.
- Resolve conflicts conservatively.
- Prefer upstream behavior unless the fork would lose a required security or runtime property.

## Current Product Customization Boundary

The goal is to keep product-specific behavior concentrated in these places:

- `hermes_cli/product_runtime_service.py`
- `toolsets.py`
- `model_tools.py`
- runtime-facing tests such as `tests/hermes_cli/test_product_runtime_service.py`

The fork should prefer:

- `SOUL.md` for runtime identity
- runtime env/config for deployment behavior

The fork should avoid growing deeper changes in:

- `run_agent.py`
- prompt assembly internals
- generic Hermes CLI/gateway behavior

## Known conflict hotspots

These are the files most likely to conflict during upstream syncs:

- `run_agent.py`
- `tests/conftest.py`
- `model_tools.py`
- `toolsets.py`
- `hermes_cli/product_runtime_service.py`

## Current identity rule

- Upstream now supports `SOUL.md` as the primary identity file.
- Runtime identity should be seeded through `HERMES_HOME/SOUL.md`.
- Runtime identity should not depend on extra branding-specific prompt overrides.

## Recommended sync procedure

1. Fetch upstream.
2. Create a temporary sync branch from local `main`.
3. Merge `upstream/main`.
4. Resolve conflicts with the smallest possible product-specific delta.
5. Run the focused product verification slice.
6. Only then merge or fast-forward back to `main`.

Example:

```powershell
git fetch upstream --prune
git checkout -b upstream-sync-YYYYMMDD
git merge upstream/main
```

## Post-merge verification

Run at minimum:

```powershell
python -m pytest tests/hermes_cli/test_product_runtime_service.py tests/agent/test_prompt_builder_identity.py -o addopts=
python -m compileall hermes_cli run_agent.py
```

If the external product app changed runtime seeding or launch behavior, also run its focused validation there:

```powershell
python -m pytest tests/test_runtime_bootstrap.py tests/test_runtime_manager.py
python -m compileall src
```

## Resolution guidance

- If upstream improves a generic mechanism the fork already customizes, prefer adopting upstream and moving the fork back toward configuration.
- If product-specific behavior can be expressed through `SOUL.md`, env, or runtime wrapper code, do not patch deeper Hermes core.
- If a change would widen the runtime authority or weaken the lockdown model, keep the fork behavior and document the reason.

## What success looks like

A good sync keeps:

- upstream core behavior largely intact
- runtime identity via `SOUL.md`
- runtime lockdown intact
- product-specific code concentrated in a small, easy-to-review surface
