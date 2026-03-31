from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from itsdangerous import TimestampSigner
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.e2e


@dataclass(frozen=True)
class LiveProductState:
    app_base_url: str
    local_app_base_url: str | None
    session_secret: str
    user: dict[str, object]


def _wsl_available() -> bool:
    return shutil.which("wsl.exe") is not None or shutil.which("wsl") is not None


def _wsl_exe() -> str:
    for candidate in ("wsl.exe", "wsl"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError("wsl.exe not found")


def _run_wsl_bash(command: str) -> str:
    distro = os.getenv("HERMES_E2E_WSL_DISTRO", "Ubuntu")
    user = os.getenv("HERMES_E2E_WSL_USER", "hermestest")
    result = subprocess.run(
        [_wsl_exe(), "-d", distro, "-u", user, "bash", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "WSL command failed")
    return result.stdout.strip()


def _load_live_product_state() -> LiveProductState:
    if not _wsl_available():
        pytest.skip("WSL is not available on this machine")

    script = r"""
source ~/.hermes/hermes-core/.venv/bin/activate
python - <<'PY'
import json
from pathlib import Path
from hermes_cli.product_app import _session_secret
from hermes_cli.product_config import load_product_config
from hermes_cli.product_stack import resolve_product_urls

users_path = Path.home() / '.hermes' / 'product' / 'bootstrap' / 'users.json'
users = json.loads(users_path.read_text())
admin = next(user for user in users if user.get('is_admin') and not user.get('disabled'))
cfg = load_product_config()
urls = resolve_product_urls(cfg)
print(json.dumps({
    'session_secret': _session_secret(),
    'app_base_url': urls['app_base_url'],
    'local_app_base_url': urls.get('local_app_base_url'),
    'user': admin,
}))
PY
"""
    payload = json.loads(_run_wsl_bash(script))
    return LiveProductState(
        app_base_url=os.getenv("HERMES_E2E_BASE_URL", str(payload["app_base_url"])),
        local_app_base_url=str(payload["local_app_base_url"]) if payload.get("local_app_base_url") else None,
        session_secret=str(payload["session_secret"]),
        user=dict(payload["user"]),
    )


def _healthcheck(base_url: str) -> None:
    script = f"curl -fsS {base_url.rstrip('/')}/healthz"
    try:
        _run_wsl_bash(script)
    except RuntimeError as exc:
        pytest.skip(f"Live product app is not healthy at {base_url}: {exc}")


def _build_session_cookie(state: LiveProductState) -> str:
    user = state.user
    payload = {
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
        "csrf_token": secrets.token_urlsafe(24),
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(state.session_secret).sign(encoded).decode("utf-8")


@pytest.fixture(scope="session")
def live_product_state() -> LiveProductState:
    state = _load_live_product_state()
    _healthcheck(state.local_app_base_url or state.app_base_url)
    return state


@pytest.fixture()
def authenticated_page(browser, live_product_state: LiveProductState) -> Page:
    context = browser.new_context(ignore_https_errors=True)
    cookie_value = _build_session_cookie(live_product_state)
    cookie_targets = [live_product_state.app_base_url]
    if live_product_state.local_app_base_url:
        cookie_targets.append(live_product_state.local_app_base_url)
    context.add_cookies(
        [
            {
                "name": "hermes_product_session",
                "value": cookie_value,
                "url": target,
                "httpOnly": True,
                "sameSite": "Lax",
            }
            for target in cookie_targets
        ]
    )
    page = context.new_page()
    page.goto(live_product_state.app_base_url, wait_until="networkidle")
    yield page
    context.close()


def test_live_product_app_loads_authenticated_shell(authenticated_page: Page) -> None:
    session = authenticated_page.evaluate(
        """async () => {
            const response = await fetch('/api/auth/session', {credentials: 'same-origin'});
            return await response.json();
        }"""
    )
    assert session["authenticated"] is True

    expect(authenticated_page.locator("#chatCard")).to_be_visible()
    expect(authenticated_page.locator("#workspaceCard")).to_be_visible()
    expect(authenticated_page.locator("#adminCard")).to_be_visible()
    expect(authenticated_page.locator("#sessionChip")).to_contain_text("Admin")


def test_live_product_workspace_upload_and_delete(authenticated_page: Page, tmp_path: Path) -> None:
    folder_name = f"e2e-folder-{int(time.time())}"
    file_name = "e2e-upload.txt"
    upload_path = tmp_path / file_name
    upload_path.write_text("live product workspace upload\n", encoding="utf-8")

    authenticated_page.locator("#workspaceFolderButton").click()
    authenticated_page.locator("#workspaceFolderName").fill(folder_name)
    authenticated_page.locator("#workspaceFolderForm").locator("button[type='submit']").click()
    expect(authenticated_page.locator("#workspaceTable")).to_contain_text(folder_name)

    file_input = authenticated_page.locator("#workspaceFileInput")
    file_input.set_input_files(str(upload_path))
    expect(authenticated_page.locator("#workspaceTable")).to_contain_text(file_name)

    authenticated_page.locator(f"button.workspace-delete-button[data-path='{file_name}']").click()
    expect(authenticated_page.locator("#workspaceTable")).not_to_contain_text(file_name)


def test_live_product_chat_turn_returns_response(authenticated_page: Page) -> None:
    chat_input = authenticated_page.locator("#chatInput")
    chat_input.fill("Reply with exactly: e2e-chat-ok")
    authenticated_page.locator("#chatSubmit").click()

    assistant_messages = authenticated_page.locator(".chat-bubble.assistant .chat-content")
    expect(assistant_messages.last).to_contain_text("e2e-chat-ok", timeout=30000)


def test_live_product_admin_can_create_invite_link(authenticated_page: Page) -> None:
    display_name = f"E2E Invite {int(time.time())}"

    authenticated_page.locator("#adminDisplayNameInput").fill(display_name)
    authenticated_page.locator("#adminCreateUserButton").click()

    expect(authenticated_page.locator("#adminSignupTokenCard")).to_be_visible()
    expect(authenticated_page.locator("#adminSignupTokenUrl")).to_contain_text("/invite/")
    expect(authenticated_page.locator("#adminUsersTable")).to_contain_text(display_name)
    expect(authenticated_page.locator("#adminUsersTable")).to_contain_text("Pending invite")
