from __future__ import annotations

import secrets
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_product.live_product_support import (
    LiveProductState,
    add_signed_session_cookie,
    build_user_session_payload,
    capture_artifact_screenshot,
    delete_runtime_for_user,
    drag_and_drop,
    healthcheck,
    load_live_product_state,
    open_authenticated_page,
    redact_page_text,
    send_chat_and_expect_reply,
    wait_for_authenticated_shell,
)


pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def live_product_state(live_product_install_state: dict[str, object]) -> LiveProductState:
    del live_product_install_state
    state = load_live_product_state()
    healthcheck(state.local_app_base_url or state.app_base_url)
    return state


@pytest.fixture()
def authenticated_page(browser, live_product_state: LiveProductState) -> Page:
    context, page = open_authenticated_page(browser, live_product_state)
    wait_for_authenticated_shell(page)
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
    capture_artifact_screenshot(authenticated_page, "authenticated-shell")


def test_live_product_workspace_upload_and_delete(authenticated_page: Page, tmp_path) -> None:
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
    capture_artifact_screenshot(authenticated_page, "workspace-with-upload")

    authenticated_page.locator(f"button.workspace-delete-button[data-path='{file_name}']").click()
    expect(authenticated_page.locator("#workspaceTable")).not_to_contain_text(file_name)


def test_live_product_workspace_supports_folder_moves_and_folder_delete(
    authenticated_page: Page, tmp_path
) -> None:
    parent_folder = f"e2e-parent-{int(time.time())}"
    child_folder = f"e2e-child-{int(time.time())}"
    file_name = "drag-target.txt"
    upload_path = tmp_path / file_name
    upload_path.write_text("drag me\n", encoding="utf-8")

    authenticated_page.locator("#workspaceFolderButton").click()
    authenticated_page.locator("#workspaceFolderName").fill(parent_folder)
    authenticated_page.locator("#workspaceFolderForm").locator("button[type='submit']").click()
    expect(authenticated_page.locator("#workspaceTable")).to_contain_text(parent_folder)

    authenticated_page.locator("#workspaceFolderButton").click()
    authenticated_page.locator("#workspaceFolderName").fill(child_folder)
    authenticated_page.locator("#workspaceFolderForm").locator("button[type='submit']").click()
    expect(authenticated_page.locator("#workspaceTable")).to_contain_text(child_folder)

    authenticated_page.locator("#workspaceFileInput").set_input_files(str(upload_path))
    expect(authenticated_page.locator("#workspaceTable")).to_contain_text(file_name)

    drag_and_drop(
        authenticated_page,
        f".workspace-entry[data-path='{file_name}']",
        f".workspace-entry[data-path='{parent_folder}'] .workspace-folder-drop-target",
    )
    expect(authenticated_page.locator("#workspaceMessage")).to_contain_text("Moved.")
    expect(authenticated_page.locator("#workspacePathLabel")).to_contain_text(f"/{parent_folder}")
    expect(authenticated_page.locator("#workspaceTable")).to_contain_text(file_name)
    capture_artifact_screenshot(authenticated_page, "workspace-drag-drop")

    drag_and_drop(
        authenticated_page,
        f".workspace-entry[data-path='{parent_folder}/{file_name}']",
        "#workspaceUpButton",
    )
    expect(authenticated_page.locator("#workspaceMessage")).to_contain_text("Moved.")
    expect(authenticated_page.locator("#workspacePathLabel")).to_have_text("Home")
    expect(authenticated_page.locator("#workspaceTable")).to_contain_text(file_name)

    authenticated_page.locator(f"button.workspace-delete-button[data-path='{child_folder}']").click()
    expect(authenticated_page.locator("#workspaceTable")).not_to_contain_text(child_folder)
    authenticated_page.locator(f"button.workspace-delete-button[data-path='{parent_folder}']").click()
    expect(authenticated_page.locator("#workspaceTable")).not_to_contain_text(parent_folder)
    authenticated_page.locator(f"button.workspace-delete-button[data-path='{file_name}']").click()
    expect(authenticated_page.locator("#workspaceTable")).not_to_contain_text(file_name)


def test_live_product_chat_turn_returns_response(authenticated_page: Page) -> None:
    token = f"e2e-chat-ok-{int(time.time())}"
    send_chat_and_expect_reply(
        authenticated_page,
        f"Reply with exactly: {token}",
        token,
    )
    capture_artifact_screenshot(authenticated_page, "chat-response")


def test_live_product_admin_can_create_invite_link(authenticated_page: Page) -> None:
    display_name = f"E2E Invite {int(time.time())}"

    authenticated_page.locator("#adminDisplayNameInput").fill(display_name)
    authenticated_page.locator("#adminCreateUserButton").click()

    expect(authenticated_page.locator("#adminSignupTokenCard")).to_be_visible()
    expect(authenticated_page.locator("#adminSignupTokenUrl")).to_contain_text("/invite/")
    expect(authenticated_page.locator("#adminUsersTable")).to_contain_text(display_name)
    expect(authenticated_page.locator("#adminUsersTable")).to_contain_text("Pending invite")
    redact_page_text(authenticated_page, ["#adminSignupTokenUrl"])
    capture_artifact_screenshot(authenticated_page, "admin-invite-created")


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
        "csrf_token": "e2e-pending-invite",
        "pending_invite_token": invite_token,
        "pending_invite_identity": {
            "sub": claimed_subject,
            "login": claimed_login,
            "name": display_name,
        },
        "auth_notice": "Confirm this Tailscale account to claim the invite.",
        "detected_tailscale_login": claimed_login,
    }
    context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
    add_signed_session_cookie(context, live_product_state, pending_payload)
    page = context.new_page()
    page.goto(live_product_state.app_base_url, wait_until="domcontentloaded")

    expect(page.locator("#authCard")).to_be_visible()
    expect(page.locator("#claimInviteButton")).to_be_visible()
    redact_page_text(page, ["#adminSignupTokenUrl"])
    capture_artifact_screenshot(page, "invite-claim-pending")
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
    capture_artifact_screenshot(page, "invite-claim-complete")
    context.close()


def test_live_product_session_persists_across_reload_and_new_tab(
    browser, live_product_state: LiveProductState
) -> None:
    context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
    add_signed_session_cookie(context, live_product_state, build_user_session_payload(live_product_state.user))

    page = context.new_page()
    page.goto(live_product_state.app_base_url, wait_until="domcontentloaded")
    wait_for_authenticated_shell(page)
    expect(page.locator("#sessionChip")).to_contain_text("Admin")

    page.reload(wait_until="domcontentloaded")
    wait_for_authenticated_shell(page)

    second_tab = context.new_page()
    second_tab.goto(live_product_state.app_base_url, wait_until="domcontentloaded")
    wait_for_authenticated_shell(second_tab)
    expect(second_tab.locator("#adminCard")).to_be_visible(timeout=15000)
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
    context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
    add_signed_session_cookie(context, live_product_state, pending_payload)
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
    capture_artifact_screenshot(authenticated_page, "admin-user-disabled")
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
    delete_runtime_for_user(str(live_product_state.user["id"]))
    token = f"e2e-runtime-recreated-{int(time.time())}"
    send_chat_and_expect_reply(
        authenticated_page,
        f"Reply with exactly: {token}",
        token,
    )
    capture_artifact_screenshot(authenticated_page, "chat-runtime-recreated")


def test_live_product_chat_stop_button_cancels_long_turn(authenticated_page: Page) -> None:
    chat_input = authenticated_page.locator("#chatInput")
    chat_input.fill("Write a numbered list from 1 to 500 with a short sentence for each item.")
    authenticated_page.locator("#chatSubmit").click()

    expect(authenticated_page.locator("#chatStop")).to_have_attribute("aria-disabled", "false")
    capture_artifact_screenshot(authenticated_page, "chat-streaming")
    authenticated_page.locator("#chatStop").click()
    expect(authenticated_page.locator("#chatMessage")).to_contain_text("Response stopped.", timeout=10000)
    capture_artifact_screenshot(authenticated_page, "chat-stopped")


def test_live_product_signed_out_shell_visual(browser, live_product_state: LiveProductState) -> None:
    context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
    page = context.new_page()
    page.goto(live_product_state.app_base_url, wait_until="domcontentloaded")

    expect(page.locator("#authCard")).to_be_visible(timeout=15000)
    capture_artifact_screenshot(page, "signed-out-shell")
    context.close()
