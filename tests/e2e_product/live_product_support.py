from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from itsdangerous import TimestampSigner
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, expect


E2E_HOME = os.getenv("HERMES_E2E_HOME", "~/.hermes-e2e-product")
E2E_INSTALL_DIR = os.getenv("HERMES_E2E_INSTALL_DIR", f"{E2E_HOME}/hermes-core")
E2E_BIN_HOME = os.getenv("HERMES_E2E_BIN_HOME", f"{E2E_HOME}/bin")
E2E_ARTIFACTS_DIR = os.getenv("HERMES_E2E_ARTIFACTS_DIR", "artifacts/e2e_product")
DEFAULT_LIVE_HOME = "~/.hermes"
PRODUCT_SECRET_KEYS = (
    "HERMES_PRODUCT_TAILSCALE_AUTH_KEY",
    "HERMES_PRODUCT_TAILSCALE_API_TOKEN",
    "HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET",
    "HERMES_PRODUCT_SESSION_SECRET",
)
REQUIRED_PRODUCT_SECRET_KEYS = PRODUCT_SECRET_KEYS[:3]


@dataclass(frozen=True)
class LiveProductState:
    app_base_url: str
    local_app_base_url: str | None
    session_secret: str
    user: dict[str, object]
    hermes_home: str
    install_dir: str
    bin_home: str


def _wsl_available() -> bool:
    if platform.system() != "Windows":
        return True
    return shutil.which("wsl.exe") is not None or shutil.which("wsl") is not None


def _wsl_exe() -> str:
    if platform.system() != "Windows":
        return "bash"
    for candidate in ("wsl.exe", "wsl"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError("wsl.exe not found")


def _wsl_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    wsl_user = os.getenv("HERMES_E2E_WSL_USER", "hermestest")
    default_home = f"/home/{wsl_user}"
    env = dict(os.environ)
    env.update(
        {
            "HERMES_E2E_HOME": _normalize_wsl_path(E2E_HOME, default_home),
            "HERMES_E2E_INSTALL_DIR": _normalize_wsl_path(E2E_INSTALL_DIR, default_home),
            "HERMES_E2E_BIN_HOME": _normalize_wsl_path(E2E_BIN_HOME, default_home),
            "HERMES_E2E_ARTIFACTS_DIR": E2E_ARTIFACTS_DIR,
            "HERMES_E2E_DEFAULT_HOME": _normalize_wsl_path(DEFAULT_LIVE_HOME, default_home),
            "HERMES_E2E_ALLOW_DEFAULT_SECRET_FALLBACK": os.getenv("HERMES_E2E_ALLOW_DEFAULT_SECRET_FALLBACK", "0"),
            "HERMES_E2E_ALLOW_DEFAULT_ADMIN_FALLBACK": os.getenv("HERMES_E2E_ALLOW_DEFAULT_ADMIN_FALLBACK", "0"),
        }
    )
    if extra_env:
        env.update(extra_env)
    return env


def _normalize_wsl_path(path: str, default_home: str) -> str:
    if path == "~":
        return default_home
    if path.startswith("~/"):
        return f"{default_home}/{path[2:]}"
    return path


def _run_wsl_bash(command: str, *, extra_env: dict[str, str] | None = None, check: bool = True) -> str:
    if platform.system() != "Windows":
        result = subprocess.run(
            ["bash", "-lc", command],
            check=False,
            capture_output=True,
            text=True,
            env=_wsl_env(extra_env),
        )
        if check and result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "WSL command failed")
        return result.stdout.strip()

    distro = os.getenv("HERMES_E2E_WSL_DISTRO", "Ubuntu")
    user = os.getenv("HERMES_E2E_WSL_USER", "hermestest")
    result = subprocess.run(
        [_wsl_exe(), "-d", distro, "-u", user, "bash", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
        env=_wsl_env(extra_env),
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "WSL command failed")
    return result.stdout.strip()


def _windows_repo_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def _wsl_repo_root() -> str:
    if platform.system() != "Windows":
        return _windows_repo_root()
    windows_repo = _windows_repo_root().replace("\\", "\\\\")
    return _run_wsl_bash(f"wslpath -a '{windows_repo}'")


def _e2e_exports() -> str:
    return textwrap.dedent(
        f"""
        export HERMES_HOME="${{HERMES_E2E_HOME:-{E2E_HOME}}}"
        export HERMES_INSTALL_DIR="${{HERMES_E2E_INSTALL_DIR:-{E2E_INSTALL_DIR}}}"
        export XDG_BIN_HOME="${{HERMES_E2E_BIN_HOME:-{E2E_BIN_HOME}}}"
        case "$HERMES_HOME" in "~/"*) HERMES_HOME="$HOME/${{HERMES_HOME#~/}}" ;; "~") HERMES_HOME="$HOME" ;; esac
        case "$HERMES_INSTALL_DIR" in "~/"*) HERMES_INSTALL_DIR="$HOME/${{HERMES_INSTALL_DIR#~/}}" ;; "~") HERMES_INSTALL_DIR="$HOME" ;; esac
        case "$XDG_BIN_HOME" in "~/"*) XDG_BIN_HOME="$HOME/${{XDG_BIN_HOME#~/}}" ;; "~") XDG_BIN_HOME="$HOME" ;; esac
        export HERMES_HOME
        export HERMES_INSTALL_DIR
        export HERMES_CORE_INSTALL_DIR="$HERMES_INSTALL_DIR"
        export XDG_BIN_HOME
        export PATH="$XDG_BIN_HOME:$PATH"
        mkdir -p "$HERMES_HOME" "$XDG_BIN_HOME"
        """
    ).strip()


def _seed_secrets_and_admin_script() -> str:
    secret_keys = ", ".join(repr(key) for key in PRODUCT_SECRET_KEYS)
    required_secret_keys = ", ".join(repr(key) for key in REQUIRED_PRODUCT_SECRET_KEYS)
    script = """
        python3 - <<'PY'
        import json
        import os
        import yaml
        from pathlib import Path

        from hermes_cli.config import save_env_value_secure
        from hermes_cli.product_config import load_product_config, save_product_config
        from hermes_cli.product_install import ensure_product_app_service_started
        from hermes_cli.product_stack import bootstrap_first_admin_enrollment, ensure_product_stack_started, initialize_product_stack, resolve_product_urls
        from hermes_cli.product_users import bootstrap_first_admin_user, list_product_users
        import subprocess

        current_home = Path(os.path.expanduser(os.environ["HERMES_E2E_DEFAULT_HOME"]))
        target_home = Path(os.path.expanduser(os.environ["HERMES_HOME"]))
        target_home.mkdir(parents=True, exist_ok=True)
        secret_keys = [__SECRET_KEYS__]
        required_secret_keys = [__REQUIRED_SECRET_KEYS__]
        allow_secret_fallback = os.environ.get("HERMES_E2E_ALLOW_DEFAULT_SECRET_FALLBACK") == "1"
        allow_admin_fallback = os.environ.get("HERMES_E2E_ALLOW_DEFAULT_ADMIN_FALLBACK") == "1"

        def _read_env_value(path: Path, key: str) -> str:
            if not path.exists():
                return ""
            for raw in path.read_text(encoding="utf-8").splitlines():
                if "=" not in raw:
                    continue
                left, right = raw.split("=", 1)
                if left.strip() == key:
                    return right.strip().strip('"').strip("'")
            return ""

        current_env = current_home / ".env"
        resolved_secrets = {}
        for key in secret_keys:
            value = str(os.environ.get(key, "")).strip()
            if not value and allow_secret_fallback:
                value = _read_env_value(current_env, key)
            if value:
                save_env_value_secure(key, value)
                resolved_secrets[key] = value

        missing_required = [key for key in required_secret_keys if not resolved_secrets.get(key)]
        if missing_required:
            raise SystemExit(
                "Missing required product E2E secrets: "
                + ", ".join(missing_required)
                + ". Set them explicitly or opt into default-home fallback."
            )

        config = load_product_config()
        config.setdefault("product", {}).setdefault("brand", {})["name"] = "Hermes Core E2E"
        config.setdefault("auth", {})["client_id"] = str(config.get("auth", {}).get("client_id") or "hermes-core-e2e").strip() or "hermes-core-e2e"
        tailscale_cfg = config.setdefault("network", {}).setdefault("tailscale", {})
        current_product_config_path = current_home / "product.yaml"
        if current_product_config_path.exists():
            current_product_config = yaml.safe_load(current_product_config_path.read_text(encoding="utf-8")) or {}
            current_tailscale_cfg = ((current_product_config.get("network") or {}).get("tailscale") or {})
            for key in ("device_name", "tailnet_name", "api_tailnet_name"):
                if current_tailscale_cfg.get(key) and not tailscale_cfg.get(key):
                    tailscale_cfg[key] = str(current_tailscale_cfg[key])
        if not tailscale_cfg.get("device_name") or not tailscale_cfg.get("tailnet_name"):
            status = json.loads(
                subprocess.check_output(["tailscale", "status", "--json"], text=True)
            )
            self_info = status.get("Self", {})
            dns_name = str(self_info.get("DNSName") or "").rstrip(".")
            if ".ts.net" in dns_name:
                host_part, _, suffix = dns_name.partition(".")
                tailnet_name = suffix[: -len(".ts.net")] if suffix.endswith(".ts.net") else suffix
                if host_part and not tailscale_cfg.get("device_name"):
                    tailscale_cfg["device_name"] = host_part
                if tailnet_name and not tailscale_cfg.get("tailnet_name"):
                    tailscale_cfg["tailnet_name"] = tailnet_name
        save_product_config(config)

        config = initialize_product_stack(config)
        ensure_product_stack_started(config)
        state = bootstrap_first_admin_enrollment(config, force_new=True)
        ensure_product_app_service_started(config)

        users = list_product_users()
        if not any(user.is_admin and not user.disabled for user in users):
            current_users_path = current_home / "product" / "bootstrap" / "users.json"
            admin_login = "e2e-admin@example.test"
            admin_subject = "userid:e2e-admin"
            admin_name = "E2E Admin"
            if allow_admin_fallback and current_users_path.exists():
                existing = json.loads(current_users_path.read_text(encoding="utf-8"))
                for row in existing:
                    if row.get("is_admin") and not row.get("disabled"):
                        admin_login = str(row.get("tailscale_login") or row.get("email") or admin_login)
                        admin_subject = str(row.get("tailscale_subject") or admin_subject)
                        admin_name = str(row.get("display_name") or row.get("username") or admin_name)
                        break
            bootstrap_first_admin_user(
                tailscale_subject=admin_subject,
                tailscale_login=admin_login,
                display_name=admin_name,
            )

        urls = resolve_product_urls(config)
        print(
            json.dumps(
                {{
                    "bootstrap_url": state.get("bootstrap_url") or state.get("setup_url"),
                    "app_base_url": urls["app_base_url"],
                    "local_app_base_url": urls.get("local_app_base_url"),
                }}
            )
        )
        PY
        """
    return textwrap.dedent(
        script.replace("__SECRET_KEYS__", secret_keys).replace("__REQUIRED_SECRET_KEYS__", required_secret_keys)
    ).strip()


def _load_live_product_state_script() -> str:
    return textwrap.dedent(
        """
        python3 - <<'PY'
        import json
        import os
        from pathlib import Path

        from hermes_cli.product_app import _session_secret
        from hermes_cli.product_config import load_product_config
        from hermes_cli.product_stack import resolve_product_urls

        hermes_home = Path(os.path.expanduser(os.environ["HERMES_HOME"]))
        users_path = hermes_home / "product" / "bootstrap" / "users.json"
        users = json.loads(users_path.read_text(encoding="utf-8"))
        admin = next(user for user in users if user.get("is_admin") and not user.get("disabled"))
        cfg = load_product_config()
        urls = resolve_product_urls(cfg)
        print(
            json.dumps(
                {
                    "session_secret": _session_secret(),
                    "app_base_url": urls["app_base_url"],
                    "local_app_base_url": urls.get("local_app_base_url"),
                    "user": admin,
                    "hermes_home": str(hermes_home),
                    "install_dir": str(Path(os.path.expanduser(os.environ["HERMES_INSTALL_DIR"]))),
                    "bin_home": str(Path(os.path.expanduser(os.environ["XDG_BIN_HOME"]))),
                }
            )
        )
        PY
        """
    ).strip()


def ensure_clean_e2e_home() -> None:
    _run_wsl_bash(
        f"""
        set -euo pipefail
        {_e2e_exports()}
        rm -rf "$HERMES_HOME" "$XDG_BIN_HOME"
        mkdir -p "$HERMES_HOME" "$XDG_BIN_HOME"
        """,
    )


def run_repo_source_install() -> None:
    repo_root = _wsl_repo_root()
    _run_wsl_bash(
        f"""
        set -euo pipefail
        {_e2e_exports()}
        rm -rf /tmp/hermes-e2e-source
        mkdir -p /tmp/hermes-e2e-source
        rsync -a --delete \
          --exclude='.git' \
          --exclude='.venv' \
          --exclude='.pytest_cache' \
          --exclude='__pycache__' \
          --exclude='.tmp' \
          --exclude='.tmp-*' \
          --exclude='build/pytesttmp*' \
          "{repo_root}/" /tmp/hermes-e2e-source/
        tr -d '\\r' < "{repo_root}/scripts/install-product.sh" > /tmp/hermes-install-product.sh
        chmod +x /tmp/hermes-install-product.sh
        bash /tmp/hermes-install-product.sh --skip-setup
        """,
        extra_env={"HERMES_CORE_SOURCE_DIR": "/tmp/hermes-e2e-source"},
    )


def run_curl_reinstall() -> None:
    _run_wsl_bash(
        f"""
        set -euo pipefail
        {_e2e_exports()}
        curl -fsSL https://github.com/ErniConcepts/hermes-agent-core/raw/refs/heads/main/scripts/install-product.sh | bash -s -- --skip-setup
        """,
    )


def run_product_uninstall() -> None:
    _run_wsl_bash(
        f"""
        set -euo pipefail
        {_e2e_exports()}
        if [ -x "$XDG_BIN_HOME/hermes-core" ]; then
          "$XDG_BIN_HOME/hermes-core" uninstall --yes || true
        fi
        rm -rf "$HERMES_HOME" "$XDG_BIN_HOME"
        """,
    )


def prepare_live_product_install() -> dict[str, Any]:
    output = _run_wsl_bash(
        f"""
        set -euo pipefail
        {_e2e_exports()}
        source "$HERMES_INSTALL_DIR/.venv/bin/activate"
        {_seed_secrets_and_admin_script()}
        """,
    )
    return json.loads(output or "{}")


def load_live_product_state() -> LiveProductState:
    if not _wsl_available():
        raise RuntimeError("WSL is not available on this machine")
    payload = json.loads(
        _run_wsl_bash(
            f"""
            set -euo pipefail
            {_e2e_exports()}
            source "$HERMES_INSTALL_DIR/.venv/bin/activate"
            {_load_live_product_state_script()}
            """
        )
    )
    return LiveProductState(
        app_base_url=os.getenv("HERMES_E2E_BASE_URL", str(payload["app_base_url"])),
        local_app_base_url=str(payload["local_app_base_url"]) if payload.get("local_app_base_url") else None,
        session_secret=str(payload["session_secret"]),
        user=dict(payload["user"]),
        hermes_home=str(payload["hermes_home"]),
        install_dir=str(payload["install_dir"]),
        bin_home=str(payload["bin_home"]),
    )


def healthcheck(base_url: str) -> None:
    _run_wsl_bash(f"curl -fsS {base_url.rstrip('/')}/healthz")


def delete_runtime_for_user(user_id: str) -> None:
    _run_wsl_bash(
        f"""
        set -euo pipefail
        {_e2e_exports()}
        source "$HERMES_INSTALL_DIR/.venv/bin/activate"
        python - <<'PY'
        from hermes_cli.product_runtime import delete_product_runtime
        delete_product_runtime({user_id!r})
        print("ok")
        PY
        """
    )


def sign_session_payload(session_secret: str, payload: dict[str, object]) -> str:
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(session_secret).sign(encoded).decode("utf-8")


def build_user_session_payload(user: dict[str, object]) -> dict[str, object]:
    return {
        "user": {
            "id": str(user["id"]),
            "sub": str(user["id"]),
            "email": str(user.get("email") or ""),
            "name": str(user.get("display_name") or user.get("username") or user["id"]),
            "preferred_username": str(user.get("username") or ""),
            "is_admin": bool(user.get("is_admin")),
            "tailscale_login": str(user.get("tailscale_login") or ""),
        },
        "user_refreshed_at": int(time.time()),
        "csrf_token": "e2e-static-csrf-token",
    }


def cookie_targets(state: LiveProductState) -> list[str]:
    targets = [state.app_base_url]
    if state.local_app_base_url:
        targets.append(state.local_app_base_url)
    return targets


def add_signed_session_cookie(context, state: LiveProductState, payload: dict[str, object]) -> None:
    cookie_value = sign_session_payload(state.session_secret, payload)
    context.add_cookies(
        [
            {
                "name": "hermes_product_session",
                "value": cookie_value,
                "url": target,
                "httpOnly": True,
                "sameSite": "Lax",
            }
            for target in cookie_targets(state)
        ]
    )


def open_authenticated_page(browser, live_product_state: LiveProductState, *, retries: int = 3) -> tuple[object, Page]:
    last_error: Exception | None = None
    for _ in range(retries):
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
        add_signed_session_cookie(context, live_product_state, build_user_session_payload(live_product_state.user))
        page = context.new_page()
        try:
            page.goto(live_product_state.app_base_url, wait_until="domcontentloaded")
            wait_for_authenticated_shell(page, timeout=20000)
            return context, page
        except PlaywrightTimeoutError as exc:
            last_error = exc
            context.close()
            continue
    raise last_error or RuntimeError("Could not open authenticated live product page")


def wait_for_authenticated_shell(page: Page, *, timeout: int = 15000) -> None:
    page.wait_for_function(
        """async () => {
            const response = await fetch('/api/auth/session', {credentials: 'same-origin'});
            const payload = await response.json();
            return payload && payload.authenticated === true;
        }""",
        timeout=timeout,
    )
    page.wait_for_function(
        """() => {
            const chatCard = document.getElementById('chatCard');
            const workspaceCard = document.getElementById('workspaceCard');
            return Boolean(chatCard && workspaceCard && !chatCard.hidden && !workspaceCard.hidden);
        }""",
        timeout=timeout,
    )
    expect(page.locator("#chatCard")).to_be_visible(timeout=timeout)
    expect(page.locator("#workspaceCard")).to_be_visible(timeout=timeout)


def send_chat_and_expect_reply(page: Page, prompt: str, expected_text: str, *, timeout: int = 30000) -> None:
    page.locator("#chatInput").fill(prompt)
    page.locator("#chatSubmit").click()
    expect(page.locator("#chatLog")).to_contain_text(expected_text, timeout=timeout)


def drag_and_drop(page: Page, source_selector: str, target_selector: str) -> None:
    data_transfer = page.evaluate_handle("new DataTransfer()")
    page.locator(source_selector).dispatch_event("dragstart", {"dataTransfer": data_transfer})
    page.locator(target_selector).dispatch_event("dragover", {"dataTransfer": data_transfer})
    page.locator(target_selector).dispatch_event("drop", {"dataTransfer": data_transfer})
    page.locator(source_selector).dispatch_event("dragend", {"dataTransfer": data_transfer})


def capture_artifact_screenshot(page: Page, name: str, *, locator: str | None = None) -> Path:
    artifact_root = Path(E2E_ARTIFACTS_DIR)
    artifact_root.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in name).strip("-") or "screenshot"
    target = artifact_root / f"{safe_name}.png"
    if locator:
        page.locator(locator).screenshot(path=str(target))
    else:
        page.screenshot(path=str(target), full_page=True)
    return target


def redact_page_text(page: Page, selectors: list[str]) -> None:
    for selector in selectors:
        page.locator(selector).evaluate_all(
            """elements => {
                for (const element of elements) {
                    if ('value' in element) {
                        element.value = '[redacted]';
                    }
                    element.textContent = '[redacted]';
                }
            }"""
        )
