# Hermes Core

`hermes-core` is a fork of [Hermes Agent](https://github.com/NousResearch/hermes-agent) focused on one use case:

run a single local device that multiple people can access through the same Tailscale tailnet, each with their own personalized agent session.

It keeps upstream Hermes as the core, and adds:

- multi-user web access through a Tailnet URL
- `tsidp` authentication through Tailscale OIDC
- per-user isolated runtimes and workspaces
- simple install/setup flow for deployment on Linux
- invite-based multi-user onboarding on the tailnet

## What This Fork Adds

The deployment layer lives primarily in `hermes_cli/product_*` and includes:

- `hermes-core install`
  - prepares a Linux host for Tailnet-only multi-user access
  - validates or installs Docker / `runsc` prerequisites on supported systems
  - installs the user-level app service and bundled `tsidp`
- `hermes-core setup`
  - configures deployment settings such as:
    - Tailscale tailnet/device detection
    - bundled `tsidp` auth key
    - `tsidp` OIDC client credentials
    - first-admin bootstrap link
    - workspace limits
- `hermes-core uninstall`
  - removes deployment data and services
  - cleans up installer-managed state

Hermes-native configuration stays on the upstream CLI:

- `hermes setup model`
- `hermes setup tools`
- `hermes setup gateway`
- `hermes setup agent`

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

- Docker is used for bundled `tsidp` and per-user runtimes
- `runsc` is the preferred runtime isolation path
- Tailscale is required
- the Tailnet URL is the only supported browser/login origin
- `tsidp` is the only product auth provider

## Quick Install

Run this as your normal user:

```bash
curl -fsSL https://raw.githubusercontent.com/ErniConcepts/hermes-agent-core/main/scripts/install-product.sh -o install-product.sh
bash install-product.sh
```

The installer is designed to:

- bootstrap Python via `uv` if needed
- install the repo into `~/.hermes/hermes-core`
- expose `hermes-core` and `hermes` launchers
- run the product install flow
- prompt for `sudo` only when host-level changes are required
- stop early if Docker is installed but the current shell still cannot use it

Do not run the installer itself with `sudo`.

If `~/.local/bin` is not on your `PATH` yet, use the full launcher path after install:

```bash
~/.local/bin/hermes-core setup
~/.local/bin/hermes setup model
```

If the installer reports that Docker is installed but your current shell cannot access it yet, refresh the shell and verify Docker before rerunning:

```bash
newgrp docker
docker info
```

## Quick Start

Typical install flow:

1. run the installer as your normal user
2. answer the product questions:
   - Tailscale auth key
   - `tsidp` hostname
   - optional SOUL template path
   - per-user workspace limit
3. let `hermes-core install` start the bundled `tsidp` service and app service
4. open the `tsidp` URL shown by setup and create a Hermes Core OIDC client
5. paste the `tsidp` client id/secret back into setup
6. open the one-time first-admin bootstrap URL from the setup summary
7. sign in with Tailscale to create the first admin account
6. configure Hermes itself with the upstream CLI:
   - `hermes setup model`
   - `hermes setup tools`
   - optional: `hermes setup gateway`
   - optional: `hermes setup agent`
7. sign into the Tailnet app URL and start using personalized agent sessions

This branch does not expose a local/LAN login surface:

- users must be in the same Tailscale tailnet
- the Tailnet URL is the only supported app URL
- invites are claimed on that Tailnet URL after Tailscale sign-in

If you enable Tailscale exposure and setup fails with `serve config denied`, grant your user permission to manage `tailscale serve` once and rerun the install/setup command:

```bash
sudo tailscale set --operator="$USER"
```

The first admin can be created before model configuration exists. In that state, auth works but chat runtimes will not answer until `hermes setup model` has been completed.

Useful commands:

```bash
hermes-core install
hermes-core install --skip-setup
hermes-core setup
hermes-core setup tailscale
hermes-core setup identity
hermes-core setup bootstrap
hermes setup model
hermes setup tools
hermes-core uninstall --yes
```

The normal `hermes-core setup` flow already includes the bootstrap/start step at the end.
`hermes-core setup bootstrap` remains available as a manual recovery command.

## Cleanup / Fresh Reinstall

To remove the product state and services:

```bash
hermes-core uninstall --yes
```

This removes the product layer, but it intentionally preserves the main Hermes config in `~/.hermes/config.yaml` and the non-product secrets in `~/.hermes/.env`.

That means a reinstall will reuse the previous model/provider configuration unless you also remove the generic Hermes config:

```bash
rm -f ~/.hermes/config.yaml
rm -f ~/.hermes/.env
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
2. `hermes-core setup` writes `~/.hermes/product.yaml`, starts bundled `tsidp`, and records its OIDC client credentials.
3. `hermes setup ...` configures Hermes-native model/tools/gateway/agent behavior in `~/.hermes/config.yaml`.
4. App service (`hermes_cli/product_app.py`) serves auth/session, chat proxy, workspace APIs, and narrow admin APIs.
5. `tsidp` provides identity through Tailscale OIDC; the app stays an OIDC client.
6. Per-user runtime containers are launched by runtime orchestration (`hermes_cli/product_runtime.py` + `hermes_cli/product_runtime_service.py`).
7. Runtime launch settings are derived from the main Hermes config, while product infrastructure comes from `product.yaml`.
8. User workspace files are written to user-scoped storage and live-mounted into the corresponding runtime.

Primary runtime surfaces:

- Product app HTTP surface (browser-facing)
- Product runtime HTTP surface (`/healthz`, `/runtime/session`, `/runtime/turn`, `/runtime/turn/stream`)
- Bundled `tsidp` service (provider-facing)

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
