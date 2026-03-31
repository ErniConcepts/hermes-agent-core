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


def _delete_runtime_for_user(user_id: str) -> None:
    script = f"""
source ~/.hermes/hermes-core/.venv/bin/activate
python - <<'PY'
from hermes_cli.product_runtime import delete_product_runtime
delete_product_runtime({user_id!r})
print('ok')
PY
"""
    _run_wsl_bash(script)


def _send_chat_and_expect_reply(page: Page, prompt: str, expected_text: str, *, timeout: int = 30000) -> None:
    page.locator("#chatInput").fill(prompt)
    page.locator("#chatSubmit").click()
    expect(page.locator("#chatLog")).to_contain_text(expected_text, timeout=timeout)


def _sign_session_payload(session_secret: str, payload: dict[str, object]) -> str:
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(session_secret).sign(encoded).decode("utf-8")


def _build_user_session_payload(user: dict[str, object]) -> dict[str, object]:
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
        "csrf_token": secrets.token_urlsafe(24),
    }


def _cookie_targets(state: LiveProductState) -> list[str]:
    targets = [state.app_base_url]
    if state.local_app_base_url:
        targets.append(state.local_app_base_url)
    return targets


def _add_signed_session_cookie(context, state: LiveProductState, payload: dict[str, object]) -> None:
    cookie_value = _sign_session_payload(state.session_secret, payload)
    context.add_cookies(
        [
            {
                "name": "hermes_product_session",
                "value": cookie_value,
                "url": target,
                "httpOnly": True,
                "sameSite": "Lax",
            }
            for target in _cookie_targets(state)
        ]
    )


@pytest.fixture(scope="session")
def live_product_state() -> LiveProductState:
    state = _load_live_product_state()
    _healthcheck(state.local_app_base_url or state.app_base_url)
    return state


@pytest.fixture()
def authenticated_page(browser, live_product_state: LiveProductState) -> Page:
    context = browser.new_context(ignore_https_errors=True)
    _add_signed_session_cookie(context, live_product_state, _build_user_session_payload(live_product_state.user))
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
    token = f"e2e-chat-ok-{int(time.time())}"
    _send_chat_and_expect_reply(
        authenticated_page,
        f"Reply with exactly: {token}",
        token,
    )


def test_live_product_admin_can_create_invite_link(authenticated_page: Page) -> None:
    display_name = f"E2E Invite {int(time.time())}"

    authenticated_page.locator("#adminDisplayNameInput").fill(display_name)
    authenticated_page.locator("#adminCreateUserButton").click()

    expect(authenticated_page.locator("#adminSignupTokenCard")).to_be_visible()
    expect(authenticated_page.locator("#adminSignupTokenUrl")).to_contain_text("/invite/")
    expect(authenticated_page.locator("#adminUsersTable")).to_contain_text(display_name)
    expect(authenticated_page.locator("#adminUsersTable")).to_contain_text("Pending invite")


def test_live_product_invite_can_be_claimed_in_second_context(
    browser, authenticated_page: Page, live_product_state: LiveProductState
) -> None:
    display_name = f"E2E Claim {int(time.time())}"
    claimed_login = f"e2e-claim-{int(time.time())}@github.idp.cheetah-vernier.ts.net"
    claimed_subject = f"userid:e2e-claim-{int(time.time())}"

    authenticated_page.locator("#adminDisplayNameInput").fill(display_name)
    authenticated_page.locator("#adminCreateUserButton").click()
    expect(authenticated_page.locator("#adminSignupTokenCard")).to_be_visible()
    signup_url = authenticated_page.locator("#adminSignupTokenUrl").text_content() or ""
    invite_token = signup_url.rstrip("/").rsplit("/", 1)[-1]
    assert invite_token

    pending_payload = {
        "csrf_token": secrets.token_urlsafe(24),
        "pending_invite_token": invite_token,
        "pending_invite_identity": {
            "sub": claimed_subject,
            "login": claimed_login,
            "name": display_name,
        },
        "auth_notice": "Confirm this Tailscale account to claim the invite.",
        "detected_tailscale_login": claimed_login,
    }
    context = browser.new_context(ignore_https_errors=True)
    _add_signed_session_cookie(context, live_product_state, pending_payload)
    page = context.new_page()
    page.goto(live_product_state.app_base_url, wait_until="networkidle")

    expect(page.locator("#authCard")).to_be_visible()
    expect(page.locator("#claimInviteButton")).to_be_visible()
    page.locator("#claimInviteButton").click()

    expect(page.locator("#chatCard")).to_be_visible()
    expect(page.locator("#workspaceCard")).to_be_visible()
    expect(page.locator("#adminCard")).to_be_hidden()
    session = page.evaluate(
        """async () => {
            const response = await fetch('/api/auth/session', {credentials: 'same-origin'});
            return await response.json();
        }"""
    )
    assert session["authenticated"] is True
    assert session["user"]["is_admin"] is False
    assert session["user"]["tailscale_login"] == claimed_login
    context.close()


def test_live_product_session_persists_across_reload_and_new_tab(
    browser, live_product_state: LiveProductState
) -> None:
    context = browser.new_context(ignore_https_errors=True)
    _add_signed_session_cookie(context, live_product_state, _build_user_session_payload(live_product_state.user))

    page = context.new_page()
    page.goto(live_product_state.app_base_url, wait_until="networkidle")
    expect(page.locator("#chatCard")).to_be_visible()
    expect(page.locator("#sessionChip")).to_contain_text("Admin")

    page.reload(wait_until="networkidle")
    expect(page.locator("#chatCard")).to_be_visible()
    expect(page.locator("#workspaceCard")).to_be_visible()

    second_tab = context.new_page()
    second_tab.goto(live_product_state.app_base_url, wait_until="networkidle")
    expect(second_tab.locator("#chatCard")).to_be_visible()
    expect(second_tab.locator("#adminCard")).to_be_visible()
    second_tab.close()
    context.close()


def test_live_product_admin_can_deactivate_claimed_user(
    browser, authenticated_page: Page, live_product_state: LiveProductState
) -> None:
    display_name = f"E2E Deactivate {int(time.time())}"
    claimed_login = f"e2e-deactivate-{int(time.time())}@github.idp.cheetah-vernier.ts.net"
    claimed_subject = f"userid:e2e-deactivate-{int(time.time())}"

    authenticated_page.locator("#adminDisplayNameInput").fill(display_name)
    authenticated_page.locator("#adminCreateUserButton").click()
    expect(authenticated_page.locator("#adminSignupTokenCard")).to_be_visible()
    signup_url = authenticated_page.locator("#adminSignupTokenUrl").text_content() or ""
    invite_token = signup_url.rstrip("/").rsplit("/", 1)[-1]
    assert invite_token

    pending_payload = {
        "csrf_token": secrets.token_urlsafe(24),
        "pending_invite_token": invite_token,
        "pending_invite_identity": {
            "sub": claimed_subject,
            "login": claimed_login,
            "name": display_name,
        },
        "auth_notice": "Confirm this Tailscale account to claim the invite.",
        "detected_tailscale_login": claimed_login,
    }
    context = browser.new_context(ignore_https_errors=True)
    _add_signed_session_cookie(context, live_product_state, pending_payload)
    page = context.new_page()
    page.goto(live_product_state.app_base_url, wait_until="networkidle")
    page.locator("#claimInviteButton").click()
    expect(page.locator("#chatCard")).to_be_visible()
    session = page.evaluate(
        """async () => {
            const response = await fetch('/api/auth/session', {credentials: 'same-origin'});
            return await response.json();
        }"""
    )
    claimed_user_id = session["user"]["id"]

    expect(authenticated_page.locator("#adminUsersTable")).to_contain_text(display_name)
    deactivate_button = authenticated_page.locator(
        f"button.admin-deactivate-button[data-user-id='{claimed_user_id}']"
    )
    expect(deactivate_button).to_be_visible(timeout=10000)
    deactivate_button.click()

    user_row = authenticated_page.locator("#adminUsersTable tr").filter(has_text=display_name).first
    expect(user_row).to_contain_text("Disabled")
    page.reload(wait_until="networkidle")
    page.wait_for_function(
        """async () => {
            const response = await fetch('/api/auth/session', {credentials: 'same-origin'});
            const payload = await response.json();
            return payload && payload.authenticated === false;
        }""",
        timeout=15000,
    )
    expect(page.locator("#authCard")).to_be_visible(timeout=15000)
    context.close()


def test_live_product_chat_recreates_runtime_after_wsl_delete(
    authenticated_page: Page, live_product_state: LiveProductState
) -> None:
    _delete_runtime_for_user(str(live_product_state.user["id"]))
    token = f"e2e-runtime-recreated-{int(time.time())}"
    _send_chat_and_expect_reply(
        authenticated_page,
        f"Reply with exactly: {token}",
        token,
    )


def test_live_product_chat_stop_button_cancels_long_turn(authenticated_page: Page) -> None:
    chat_input = authenticated_page.locator("#chatInput")
    chat_input.fill("Write a numbered list from 1 to 500 with a short sentence for each item.")
    authenticated_page.locator("#chatSubmit").click()

    expect(authenticated_page.locator("#chatStop")).to_have_attribute("aria-disabled", "false")
    authenticated_page.locator("#chatStop").click()
    expect(authenticated_page.locator("#chatMessage")).to_contain_text("Response stopped.", timeout=10000)
