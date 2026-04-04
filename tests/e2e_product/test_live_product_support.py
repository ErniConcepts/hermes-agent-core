from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tests.e2e_product import live_product_support as support


def test_cookie_targets_include_local_base_url() -> None:
    state = support.LiveProductState(
        app_base_url="https://app.example.ts.net",
        local_app_base_url="https://127.0.0.1:8443",
        session_secret="secret",
        user={"id": "1"},
        hermes_home="~/.hermes-e2e-product",
        install_dir="~/.hermes-e2e-product/hermes-core",
        bin_home="~/.hermes-e2e-product/bin",
    )

    assert support.cookie_targets(state) == [
        "https://app.example.ts.net",
        "https://127.0.0.1:8443",
    ]


def test_build_user_session_payload_uses_profile_fields() -> None:
    payload = support.build_user_session_payload(
        {
            "id": "user-7",
            "email": "user@example.test",
            "display_name": "Example User",
            "username": "example",
            "is_admin": True,
            "tailscale_login": "example@example.test",
        }
    )

    assert payload["user"] == {
        "id": "user-7",
        "sub": "user-7",
        "email": "user@example.test",
        "name": "Example User",
        "preferred_username": "example",
        "is_admin": True,
        "tailscale_login": "example@example.test",
    }
    assert payload["csrf_token"] == "e2e-static-csrf-token"


def test_capture_artifact_screenshot_sanitizes_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(support, "E2E_ARTIFACTS_DIR", str(tmp_path))
    calls: list[tuple[str, str, bool]] = []

    class FakePage:
        def screenshot(self, *, path: str, full_page: bool) -> None:
            calls.append(("page", path, full_page))
            Path(path).write_text("png", encoding="utf-8")

    target = support.capture_artifact_screenshot(FakePage(), "chat/streaming state")

    assert target == tmp_path / "chat-streaming-state.png"
    assert target.exists()
    assert calls == [("page", str(target), True)]


def test_capture_artifact_screenshot_locator_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(support, "E2E_ARTIFACTS_DIR", str(tmp_path))
    calls: list[tuple[str, str]] = []

    class FakeLocator:
        def screenshot(self, *, path: str) -> None:
            calls.append(("locator", path))
            Path(path).write_text("png", encoding="utf-8")

    class FakePage:
        def locator(self, selector: str) -> FakeLocator:
            calls.append(("selector", selector))
            return FakeLocator()

    target = support.capture_artifact_screenshot(FakePage(), "admin-card", locator="#adminCard")

    assert target == tmp_path / "admin-card.png"
    assert target.exists()
    assert calls == [("selector", "#adminCard"), ("locator", str(target))]


def test_run_wsl_bash_passes_e2e_env(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, check, capture_output, text, env):
        captured["args"] = args
        captured["env"] = env
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    if support.platform.system() == "Windows":
        monkeypatch.setattr(support, "_wsl_exe", lambda: "wsl.exe")
        expected_args = ["wsl.exe", "-d", "Ubuntu", "-u", "hermestest", "bash", "-lc", "echo ok"]
    else:
        expected_args = ["bash", "-lc", "echo ok"]
    monkeypatch.setattr(support.subprocess, "run", fake_run)

    result = support._run_wsl_bash("echo ok", extra_env={"EXTRA_FLAG": "1"})

    assert result == "ok"
    assert captured["args"] == expected_args
    assert captured["env"]["HERMES_E2E_HOME"] == support._normalize_wsl_path(
        support.E2E_HOME,
        "/home/hermestest",
    )
    assert captured["env"]["HERMES_E2E_TAILNET_NAME"] == ""
    assert captured["env"]["HERMES_E2E_DEVICE_NAME"] == ""
    assert captured["env"]["EXTRA_FLAG"] == "1"


def test_wsl_env_defaults_leave_explicit_tailnet_settings_empty() -> None:
    env = support._wsl_env()

    assert env["HERMES_E2E_TAILNET_NAME"] == ""
    assert env["HERMES_E2E_DEVICE_NAME"] == ""
    assert env["HERMES_E2E_API_TAILNET_NAME"] == ""


def test_run_wsl_bash_raises_on_failure(monkeypatch) -> None:
    def fake_run(args, check, capture_output, text, env):
        del args, check, capture_output, text, env
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    try:
        support._run_wsl_bash("exit 1")
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("Expected RuntimeError")


def test_redact_page_text_overwrites_matching_selectors() -> None:
    calls: list[tuple[str, object]] = []

    class FakeLocator:
        def evaluate_all(self, script: str) -> None:
            calls.append(("evaluate_all", script))

    class FakePage:
        def locator(self, selector: str) -> FakeLocator:
            calls.append(("locator", selector))
            return FakeLocator()

    support.redact_page_text(FakePage(), ["#adminSignupTokenUrl", "#secret"])

    assert calls[0] == ("locator", "#adminSignupTokenUrl")
    assert calls[2] == ("locator", "#secret")
    assert calls[1][0] == "evaluate_all"
    assert "[redacted]" in calls[1][1]
