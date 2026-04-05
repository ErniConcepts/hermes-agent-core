# Hermes Core

`hermes-core` is a fork of [Hermes Agent](https://github.com/NousResearch/hermes-agent) for one deployment shape:

run one Linux host that multiple people access through the same Tailscale tailnet, each with their own Hermes account, workspace, and isolated runtime.

It keeps upstream Hermes as the agent core and adds:

- multi-user web access through a Tailnet URL
- `tsidp` authentication through Tailscale OIDC
- per-user isolated runtimes and workspaces
- Linux-first install/setup flow
- invite-based onboarding on the same tailnet

![Hermes Core Tailnet Architecture](docs/fork/architecture-diagram.png)

## What This Fork Adds

The product layer lives primarily in `hermes_cli/product_*` and adds:

- `hermes-core install`
  - prepares a Linux host for Tailnet-only multi-user access
  - validates or installs Docker / `runsc` prerequisites on supported systems
  - installs the user-level app service and bundled `tsidp`
- `hermes-core setup`
  - configures deployment settings such as:
    - Tailscale node/tailnet detection
    - bundled `tsidp` auth key
    - Tailscale API token for automatic policy patching
    - automatic `tsidp` OIDC client registration
    - first-admin bootstrap link
    - workspace limits
- `hermes-core uninstall`
  - removes deployment data and services
  - cleans up installer-managed state

Hermes-native configuration stays on the upstream CLI:

- `hermes setup model`
- `hermes setup tools` (optional when you want more than the default runtime toolset)
- `hermes setup agent`

The authenticated web surface stays intentionally narrow:

- sign-in
- chat
- user workspace
- small admin user-management surface

The web runtime is intentionally narrower than the full Hermes CLI/runtime surface. By default it enables only:

- `file`
- `terminal`
- `memory`

The web runtime also follows Hermes-native session behavior:

- it uses the normal Hermes session transcript rather than a product-only reduced-history layer
- it honors the standard `session_reset` policy from `~/.hermes/config.yaml`
- session rollover therefore follows the same config-driven model used by Hermes gateway sessions

## Status

Current target:

- Linux first
- Ubuntu/Debian are the supported installer target today
- Windows is fine for development, but not the deployment target

Current deployment assumptions:

- Docker is used for bundled `tsidp` and per-user runtimes
- `runsc` is the preferred runtime isolation path
- Tailscale is required
- the Tailnet URL is the only supported browser/login origin
- `tsidp` is the only product auth provider

## Quick Install

Run this as your normal user:

```bash
curl -fsSL https://github.com/ErniConcepts/hermes-agent-core/raw/refs/heads/main/scripts/install-product.sh -o install-product.sh
bash install-product.sh
```

The installer is designed to:

- bootstrap Python via `uv` if needed
- install the repo into `~/.hermes/hermes-core`
- expose `hermes-core` and `hermes` launchers
- run the product install flow
- prompt for `sudo` only when host-level changes are required
- stop early if Docker is installed but the current shell still cannot use it
- register a dedicated Docker bridge for product runtimes
- install host firewall rules so product runtimes can reach the configured local inference port without inheriting broad host-network access

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

Typical operator flow:

1. run the installer as your normal user
2. let `hermes-core setup` detect the current Tailscale device and tailnet
3. enter:
   - Tailscale auth key for the bundled `tsidp` node
   - Tailscale API token so setup can patch tailnet policy automatically
4. let setup patch the tailnet policy and start the bundled `tsidp` service
5. let setup automatically create or reuse the Hermes Core OIDC client in `tsidp`
6. only if automatic registration fails, follow the printed fallback instructions and paste the `tsidp` client id/secret back into setup
7. finish the remaining product questions:
   - product title shown in the web UI
   - optional SOUL template path
   - per-user workspace limit
8. open the one-time first-admin bootstrap URL from the setup summary
9. sign in with Tailscale to create the first admin account
10. configure Hermes itself with the upstream CLI:
   - `hermes setup model`
   - optional: `hermes setup tools`
   - optional: `hermes setup agent`
11. sign into the Tailnet app URL and start using per-user agent sessions

## Setup Inputs

`hermes-core setup` currently requires these operator inputs:

- required:
  - Tailnet must already be available on the host
  - Tailscale auth key for the bundled `tsidp` node
  - Tailscale API token for automatic `tsidp` policy setup
- prompted by setup:
  - product title shown in the web UI
  - optional SOUL template path
  - per-user workspace limit

What setup automates:

- it detects the current Tailscale device and MagicDNS suffix
- it uses the fixed `idp.<tailnet>.ts.net` hostname for the bundled `tsidp` issuer
- it uses the API token to add the required `tailscale.com/cap/tsidp` grants, including dynamic client registration
- it automatically creates the Hermes Core OIDC client through `tsidp` when no saved client is present
- it keeps the saved OIDC client on later runs by default
- it falls back to the manual `tsidp` UI flow only when automatic registration is unavailable or rejected

## First Admin Bootstrap

Current first-admin flow:

1. `hermes-core setup` starts `tsidp` and prints the Tailnet app URL and `tsidp` issuer URL.
2. Setup automatically creates or reuses the Hermes Core OIDC client in `tsidp`.
3. If automatic registration is unavailable, setup falls back to the manual `tsidp` UI flow and prompts for `client_id` and `client_secret`.
4. Setup generates a one-time bootstrap link on the Tailnet app URL.
5. Open that exact bootstrap link in a browser that can access the same tailnet.
6. Sign in with Tailscale through `tsidp`.
7. The first successful login through that bootstrap link becomes the first admin account.

If setup is rerun after the first admin already exists, it now lets the operator choose whether to keep the current admin state or generate a new one-time bootstrap link.

Properties of this flow:

- there is no localhost or LAN fallback login
- the bootstrap link is one-time and server-tracked
- the admin account is created only after a successful `tsidp` login on the Tailnet URL
- the product app uses a dedicated session secret, not the OIDC client secret

## Invite Flow

Current invited-user flow:

1. The admin signs into the Tailnet app URL.
2. In the admin screen, the admin creates an invite link with a display name.
3. The app generates a one-time claim URL on the Tailnet app host.
4. The invited person opens that URL from a browser/device that can access the same tailnet.
5. They sign in with Tailscale through `tsidp`.
6. The app shows the detected Tailscale identity and asks for explicit confirmation.
7. After confirmation, the invite is claimed and the product user record is created.

Important behavior:

- invites are not claimed automatically anymore
- if the browser comes back as an already existing Hermes Core user, the invite is rejected instead of silently signing that user in
- invites are tailnet-only and identity-bound at claim time

## Tailnet Requirements

This product assumes:

- the host is already joined to a Tailscale tailnet
- MagicDNS is available
- `tailscale serve` can be managed by the install user
- `tsidp` is allowed by the tailnet policy

Normal setup handles the `tsidp` policy grant automatically through the Tailscale API token.

If the `tsidp` UI still says `Access denied: application capability not granted` after setup, the API token likely could not update the tailnet policy or did not have sufficient admin permission.

There is no local/LAN login surface:

- users must be in the same Tailscale tailnet
- the Tailnet URL is the only supported app URL
- invites are claimed on that Tailnet URL after Tailscale sign-in

If setup fails with `serve config denied`, grant your user permission to manage `tailscale serve` once and rerun the install/setup command:

```bash
sudo tailscale set --operator="$USER"
```

The first admin can be created before model configuration exists. In that state, auth works but chat runtimes will not answer until `hermes setup model` has been completed.

The product app keeps `/healthz` intentionally minimal for unauthenticated liveness checks and does not expose internal issuer/app URLs there.

Useful commands:

```bash
hermes-core install
hermes-core install --skip-setup
hermes-core setup
hermes-core setup tailscale
hermes-core setup identity
hermes-core setup bootstrap
hermes setup model
hermes setup tools   # optional if you want more than file/terminal/memory
hermes-core uninstall --yes
```

The normal `hermes-core setup` flow already includes the bootstrap/start step at the end.
`hermes-core setup bootstrap` remains available as a manual recovery command.

## Runtime Identity

By default, the product runtime uses a generated product-specific `SOUL.md`. It tells the agent:

- it is running in a per-user product runtime
- persistent user-visible work belongs in `/workspace`
- `/workspace/.tmp` is available for scratch/intermediate work but is not part of the normal user-facing workspace
- actual capabilities come only from the enabled runtime toolsets

Operators can override that runtime identity during `hermes-core setup` by providing a custom runtime SOUL template path. This is separate from the normal CLI `~/.hermes/SOUL.md`, which still controls standalone `hermes` sessions.

## Runtime Sessions

Per-user web runtimes use the normal Hermes session transcript and standard Hermes session-reset policy.

If you want automatic runtime rollover behavior, configure it through normal Hermes config in `~/.hermes/config.yaml`:

- `session_reset.mode`
- `session_reset.idle_minutes`
- `session_reset.at_hour`

Those settings are staged into per-user runtimes and applied there, instead of using a separate product-only history compaction layer.

## Live E2E Cleanup

The live WSL/browser E2E suite creates temporary invites and users. To remove those records from the current product state, run:

```bash
scripts/cleanup-product-e2e-state.sh
```

By default the script targets `~/.hermes/product/bootstrap`. You can override the target home with `HERMES_HOME=/path/to/home`.

## Live Product E2E

The product live E2E suite now covers:

- clean install into an isolated WSL home
- bootstrap/setup preparation using current product secrets when available
- click-driven browser coverage of chat, workspace, admin, invite, disable, and runtime recreation flows
- screenshot capture for the major UI states
- uninstall plus curl-based reinstall recovery

The suite is intentionally isolated from your normal product install. By default it uses:

- `HERMES_HOME=~/.hermes-e2e-product`
- install dir `~/.hermes-e2e-product/hermes-core`
- bin dir `~/.hermes-e2e-product/bin`
- screenshot/artifact dir `artifacts/e2e_product/`

Required local inputs:

- a reachable WSL distro and user
- Docker/Tailscale prerequisites inside that WSL environment
- product secrets in the current shell:
  - `HERMES_PRODUCT_TAILSCALE_AUTH_KEY`
  - `HERMES_PRODUCT_TAILSCALE_API_TOKEN`
  - `HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET`
- tailnet routing metadata for the isolated install:
  - `HERMES_E2E_DEVICE_NAME`
  - `HERMES_E2E_TAILNET_NAME`
  - optional: `HERMES_E2E_API_TAILNET_NAME`

Safer default behavior:

- the suite does not silently copy secrets from the default WSL `~/.hermes/.env`
- the suite does not clone the default install's admin identity into the isolated E2E home
- the suite does not infer tailnet routing from the operator's existing product config
- screenshot artifacts redact invite-token UI fields before writing PNGs

Optional overrides:

- `HERMES_E2E_WSL_DISTRO`
- `HERMES_E2E_WSL_USER`
- `HERMES_E2E_BASE_URL`
- `HERMES_E2E_DEVICE_NAME`
- `HERMES_E2E_TAILNET_NAME`
- `HERMES_E2E_API_TAILNET_NAME`
- `HERMES_E2E_HOME`
- `HERMES_E2E_INSTALL_DIR`
- `HERMES_E2E_BIN_HOME`
- `HERMES_E2E_ARTIFACTS_DIR`

Run it locally with:

```bash
source venv/bin/activate
python -m pytest -o addopts="" tests/e2e_product -q --tb=short
```

The fast suite stays in `.github/workflows/tests.yml`. The heavy live WSL/browser lane runs from `.github/workflows/product-live-e2e.yml` on pushes to `main`.

## Rerunning Setup

Rerunning `hermes-core setup` is safe and is the normal way to refresh product configuration.

On reruns:

- Tailscale detection is repeated from the current host/tailnet state
- pressing Enter keeps the existing saved Tailscale auth key and API token
- the `tsidp` client is reused automatically when a saved client id and secret already exist
- manual client prompts only appear if automatic `tsidp` registration is unavailable
- if the first admin already exists, setup offers:
  - keep the current admin bootstrap state
  - or generate a fresh bootstrap link
- if bootstrap state is inconsistent, setup repairs it by generating a fresh bootstrap link

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
- `hermes_cli/product_chat_transport.py`
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

Current high-level flow:

1. `hermes-core install` prepares host prerequisites, runtime networking, and product services.
2. `hermes-core setup` writes `~/.hermes/product.yaml`, starts bundled `tsidp`, and records product auth/bootstrap state.
3. `hermes setup ...` configures Hermes-native model, tool, and agent behavior in `~/.hermes/config.yaml`.
4. `product_app.py` serves the browser-facing auth, session, chat, workspace, and narrow admin routes.
5. `product_chat_transport.py` keeps the web chat path thin and routes it into the runtime layer.
6. `product_runtime.py` stages or refreshes the per-user install, then ensures the user container is healthy.
7. `product_runtime_service.py` runs inside the per-user container and reuses `AIAgent` against the staged Hermes home.
8. User workspace files are stored server-side and live-mounted into the matching runtime container.

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

Use the public README for install/use guidance, then the fork docs for maintenance details:

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
