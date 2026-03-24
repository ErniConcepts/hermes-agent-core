# Hermes Core

`hermes-core` is a fork of [Hermes Agent](https://github.com/NousResearch/hermes-agent) focused on one use case:

run a single local device that multiple people on your network can access, each with their own personalized agent session.

It keeps upstream Hermes as the core, and adds:

- multi-user web access on a local host
- Pocket ID authentication
- per-user isolated runtimes and workspaces
- simple install/setup flow for deployment on Linux
- optional Tailscale exposure for remote tailnet access

## What This Fork Adds

The deployment layer lives primarily in `hermes_cli/product_*` and includes:

- `hermes-core install`
  - prepares a Linux host for local multi-user access
  - validates or installs Docker / `runsc` prerequisites on supported systems
  - installs user-level app/auth services
- `hermes-core setup`
  - configures deployment settings such as:
    - public host
    - optional Tailscale mode
    - Pocket ID bootstrap
    - model route
    - runtime toolsets
    - workspace limits
- `hermes-core uninstall`
  - removes deployment data and services
  - cleans up installer-managed state

The authenticated web surface is intentionally narrow:

- sign-in
- chat
- user workspace
- small admin user-management surface

## Status

Current target:

- Linux first
- Ubuntu/Debian are the supported installer target today
- Windows is fine for development, but not the production target

Current deployment assumptions:

- Docker is used for Pocket ID and per-user runtimes
- `runsc` is the preferred runtime isolation path
- Pocket ID is the local auth provider
- Tailscale is optional and, when enabled, becomes the single canonical browser/login origin

## Quick Install

Run this as your normal user:

```bash
curl -fsSL https://raw.githubusercontent.com/ErniConcepts/hermes-agent-core/main/scripts/install-product.sh -o install-product.sh
bash install-product.sh
```

The installer is designed to:

- bootstrap Python via `uv` if needed
- install the repo into `~/.hermes/hermes-core`
- expose a `hermes-core` launcher
- run the product install flow
- prompt for `sudo` only when host-level changes are required

Do not run the installer itself with `sudo`.

If `~/.local/bin` is not on your `PATH` yet, use the full launcher path after install:

```bash
~/.local/bin/hermes-core setup
```

## Quick Start

After install:

```bash
hermes-core setup
```

Typical flow:

1. choose the host users in your network will open
2. enable or disable Tailscale exposure
3. choose the model route
4. choose the runtime toolsets
5. let setup start Pocket ID and the app services
6. open the first-admin setup URL
7. sign in and start using personalized agent sessions

Useful commands:

```bash
hermes-core install
hermes-core install --skip-setup
hermes-core setup
hermes-core setup network
hermes-core setup model
hermes-core setup bootstrap
hermes-core uninstall --yes
```

The normal `hermes-core setup` flow already includes the bootstrap/start step at the end.
`hermes-core setup bootstrap` remains available as a manual recovery command.

## Cleanup / Fresh Reinstall

To remove the product state and services:

```bash
hermes-core uninstall --yes
```

Then rerun the installer command above.

## Repo Layout

The fork-specific product layer is mainly here:

- `hermes_cli/product_app.py`
- `hermes_cli/product_config.py`
- `hermes_cli/product_install.py`
- `hermes_cli/product_oidc.py`
- `hermes_cli/product_runtime.py`
- `hermes_cli/product_runtime_service.py`
- `hermes_cli/product_setup.py`
- `hermes_cli/product_stack.py`

Upstream Hermes functionality still exists in the repo and remains the foundation for:

- the core `AIAgent`
- tool calling
- CLI infrastructure
- model/provider integrations
- memory and session search
- gateway/platform integrations

The fork policy is to prefer sidecar adaptation over modifying upstream Hermes files unless an upstream-facing change is explicitly intended.

## Architecture Flow

Current high-level runtime flow:

1. `hermes-core install` prepares host prerequisites and installs product services.
2. `hermes-core setup` writes `~/.hermes/product.yaml` and bootstraps Pocket ID + OIDC client.
3. App service (`hermes_cli/product_app.py`) serves auth/session, chat proxy, workspace APIs, and narrow admin APIs.
4. Pocket ID provides identity and signup-token onboarding; the app stays an OIDC client.
5. Per-user runtime containers are launched by runtime orchestration (`hermes_cli/product_runtime.py` + `hermes_cli/product_runtime_service.py`).
6. User workspace files are written to user-scoped storage and live-mounted into the corresponding runtime.

Primary runtime surfaces:

- Product app HTTP surface (browser-facing)
- Product runtime HTTP surface (`/healthz`, `/runtime/session`, `/runtime/turn`, `/runtime/turn/stream`)
- Pocket ID service (provider-facing, proxied/controlled by product layer)

## Fork File Map

Main fork-owned code and assets (current):

- Product app + APIs:
  - `hermes_cli/product_app.py`
  - `hermes_cli/product_web.py`
  - `hermes_cli/product_web_template.py`
  - `hermes_cli/product_web_style.py`
  - `hermes_cli/product_web_script.py`
- Product auth + users:
  - `hermes_cli/product_oidc.py`
  - `hermes_cli/product_users.py`
  - `hermes_cli/product_invites.py`
  - `hermes_cli/product_identity.py`
- Product runtime + storage:
  - `hermes_cli/product_runtime.py`
  - `hermes_cli/product_runtime_service.py`
  - `hermes_cli/product_workspace.py`
  - `hermes_cli/product_config.py`
  - `hermes_cli/product_stack.py`
- Product CLI commands:
  - `hermes_cli/product_main.py`
  - `hermes_cli/product_install.py`
  - `hermes_cli/product_setup.py`
- Installer/runtime packaging:
  - `scripts/install-product.sh`
  - `Dockerfile.product`
- Fork maintainer docs:
  - `docs/fork/DEVELOPMENT.md`
  - `docs/fork/SPEC.md`
  - `docs/fork/UPSTREAM-SYNC.md`
- Fork product tests:
  - `tests/hermes_cli/test_product_*.py`

## Development

Product development notes live in:

- [DEVELOPMENT.md](docs/fork/DEVELOPMENT.md)
- [SPEC.md](docs/fork/SPEC.md)
- [UPSTREAM-SYNC.md](docs/fork/UPSTREAM-SYNC.md)
- [PRE_PUBLISH_AUDIT.md](docs/fork/PRE_PUBLISH_AUDIT.md)

Run the product test suite with:

```bash
source venv/bin/activate
python -m pytest tests/hermes_cli/test_product_*.py -q
```

## Publish Notes

Before treating this as a stable public release, the main areas to keep tightening are:

- installer UX and distro coverage beyond Ubuntu/Debian
- setup UX for model/tool selection in more headless automation cases
- release/versioning flow for the public installer
- user-facing docs outside the maintainer notes
- broader end-to-end Linux acceptance coverage across more host shapes

## License

This fork remains under the upstream project license. See [LICENSE](LICENSE).
