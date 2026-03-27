import json
from argparse import Namespace
from unittest.mock import patch

import pytest

from hermes_cli.product_config import load_product_config
from hermes_cli.product_setup import (
    run_product_setup_wizard,
    setup_product_identity,
    setup_product_network,
    setup_product_storage,
    setup_product_tailscale,
)


def _make_product_args(**overrides):
    return Namespace(
        non_interactive=overrides.get("non_interactive", False),
        section=overrides.get("section", None),
        from_install=overrides.get("from_install", False),
    )


def test_product_setup_rejects_removed_model_section(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True):
        run_product_setup_wizard(_make_product_args(section="model"))

    assert not (tmp_path / "config.yaml").exists()


def test_product_setup_network_section_updates_public_host(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: "officebox.local")
    monkeypatch.setattr("hermes_cli.product_setup._validate_public_host_for_this_machine", lambda host: None)

    setup_product_network()

    product_config = load_product_config()
    assert product_config["network"]["public_host"] == "officebox.local"


def test_product_setup_network_section_strips_terminal_escape_sequences(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.product_setup.prompt",
        lambda *args, **kwargs: "\x1b[A\x1b[Alocalhost\x1b[B",
    )
    monkeypatch.setattr("hermes_cli.product_setup._validate_public_host_for_this_machine", lambda host: None)

    setup_product_network()

    product_config = load_product_config()
    assert product_config["network"]["public_host"] == "localhost"


def test_product_setup_network_section_rejects_host_resolving_to_other_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    answers = iter(["LaptopJannis.local", "localhost"])
    warnings = []
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(
        "hermes_cli.product_setup._validate_public_host_for_this_machine",
        lambda host: (_ for _ in ()).throw(
            ValueError("Host 'LaptopJannis.local' resolves to 192.168.1.155, not this machine (192.168.1.27).")
        )
        if host == "LaptopJannis.local"
        else None,
    )
    monkeypatch.setattr("hermes_cli.product_setup.print_warning", lambda message: warnings.append(message))

    setup_product_network()

    product_config = load_product_config()
    assert product_config["network"]["public_host"] == "localhost"
    assert any("resolves to 192.168.1.155" in message for message in warnings)


def test_product_setup_network_section_warns_when_host_does_not_resolve_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    warnings = []
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: "officebox.local")
    monkeypatch.setattr(
        "hermes_cli.product_setup._validate_public_host_for_this_machine",
        lambda host: "Host 'officebox.local' does not currently resolve on this machine. LAN access will work only after DNS or mDNS points that name at this device.",
    )
    monkeypatch.setattr("hermes_cli.product_setup.print_warning", lambda message: warnings.append(message))

    setup_product_network()

    product_config = load_product_config()
    assert product_config["network"]["public_host"] == "officebox.local"
    assert any("does not currently resolve" in message for message in warnings)


def test_product_setup_identity_section_updates_soul_template_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    template_path = tmp_path / "custom-soul.md"
    template_path.write_text("custom soul", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: str(template_path))

    setup_product_identity()

    product_config = load_product_config()
    assert product_config["product"]["agent"]["soul_template_path"] == str(template_path.resolve())


def test_product_setup_storage_section_updates_workspace_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: "5")

    setup_product_storage()

    product_config = load_product_config()
    assert product_config["storage"]["user_workspace_limit_mb"] == 5120


def test_product_setup_tailscale_section_updates_tailnet_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    answers = iter(["yes", "", "", "443", "4444"])
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(
        "hermes_cli.product_setup.subprocess.run",
        lambda *args, **kwargs: type(
            "_Result",
            (),
            {
                "stdout": '{"Self":{"DNSName":"hermes-box.corpnet.ts.net."},"MagicDNSSuffix":"corpnet.ts.net"}',
            },
        )(),
    )

    setup_product_tailscale()

    product_config = load_product_config()
    assert product_config["network"]["tailscale"] == {
        "enabled": True,
        "tailnet_name": "corpnet",
        "device_name": "hermes-box",
        "app_https_port": 443,
        "auth_https_port": 4444,
        "command_path": "tailscale",
    }


def test_product_setup_tailscale_section_reports_missing_cli_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    answers = iter(["yes"])
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: next(answers))

    def _missing(*args, **kwargs):
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr("hermes_cli.product_setup.subprocess.run", _missing)

    with pytest.raises(RuntimeError, match="Tailscale CLI not found"):
        setup_product_tailscale()


def test_product_setup_tailscale_section_strips_terminal_escape_sequences(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    answers = iter(["\x1b[Ayes", "\x1b[A", "corpnet", "\x1b[B", "hermes-box", "443", "4444"])
    monkeypatch.setattr("hermes_cli.product_setup.prompt", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(
        "hermes_cli.product_setup.subprocess.run",
        lambda *args, **kwargs: type(
            "_Result",
            (),
            {
                "stdout": '{"Self":{"DNSName":"hermes-box.corpnet.ts.net."},"MagicDNSSuffix":"corpnet.ts.net"}',
            },
        )(),
    )

    setup_product_tailscale()

    product_config = load_product_config()
    assert product_config["network"]["tailscale"]["enabled"] is True
    assert product_config["network"]["tailscale"]["tailnet_name"] == "corpnet"
    assert product_config["network"]["tailscale"]["device_name"] == "hermes-box"


def test_start_product_stack_ensures_linux_product_app_service(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "hermes_cli.product_setup.ensure_product_app_service_started",
        lambda config=None: seen.append("service"),
    )
    monkeypatch.setattr("hermes_cli.product_setup.ensure_product_stack_started", lambda: seen.append("stack"))
    monkeypatch.setattr(
        "hermes_cli.product_setup.bootstrap_first_admin_enrollment",
        lambda: seen.append("bootstrap")
        or {
            "username": "admin",
            "display_name": "Administrator",
            "email": "",
            "auth_mode": "passkey",
            "bootstrap_mode": "native_setup",
            "setup_url": "https://example.ts.net:8443/setup",
            "oidc_client_id": "hermes-core",
            "first_admin_login_seen": False,
            "bootstrap_completed_at": None,
        },
    )

    from hermes_cli.product_setup import _start_product_stack

    _start_product_stack()

    assert seen == ["stack", "bootstrap", "service"]


def test_product_setup_noninteractive_prints_guidance(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with patch("hermes_cli.product_setup.is_interactive_stdin", return_value=False):
        run_product_setup_wizard(_make_product_args())

    out = capsys.readouterr().out
    assert "hermes config set model.provider custom" in out


def test_product_setup_summary_includes_first_admin_signup_url(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CORE_INSTALL_DIR", str(tmp_path / "checkout"))
    from hermes_cli.product_config import save_product_config

    product_config = load_product_config()
    save_product_config(product_config)
    state_path = tmp_path / "product" / "bootstrap" / "first_admin_enrollment.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"setup_url": "http://localhost:1411/setup"}),
        encoding="utf-8",
    )

    from hermes_cli.product_setup import _print_product_setup_summary

    _print_product_setup_summary()

    out = capsys.readouterr().out
    assert f"Install dir:    {tmp_path / 'checkout'}" in out
    assert "First admin sign-up:" in out
    assert "http://localhost:1411/setup" in out
    assert "hermes setup model" in out
    assert "hermes setup tools" in out
    assert "hermes setup gateway" in out


def test_product_setup_summary_hides_first_admin_signup_url_after_completion(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.product_config import save_product_config

    product_config = load_product_config()
    save_product_config(product_config)
    state_path = tmp_path / "product" / "bootstrap" / "first_admin_enrollment.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "bootstrap_mode": "native_setup",
                "setup_url": "http://localhost:1411/setup",
                "first_admin_login_seen": True,
                "bootstrap_completed_at": 1710000000,
            }
        ),
        encoding="utf-8",
    )

    from hermes_cli.product_setup import _print_product_setup_summary

    _print_product_setup_summary()

    out = capsys.readouterr().out
    assert "First admin bootstrap:  completed" in out
    assert "First admin sign-up:" not in out
    assert "http://localhost:1411/setup" not in out


def test_product_setup_summary_explains_tailnet_auth_is_pending_during_bootstrap(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.product_config import save_product_config

    product_config = load_product_config()
    product_config["network"]["tailscale"]["enabled"] = True
    product_config["network"]["tailscale"]["tailnet_name"] = "corpnet"
    product_config["network"]["tailscale"]["device_name"] = "hermes-box"
    product_config["network"]["public_host"] = "localhost"
    save_product_config(product_config)
    state_path = tmp_path / "product" / "bootstrap" / "first_admin_enrollment.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "bootstrap_mode": "native_setup",
                "setup_url": "http://localhost:1411/setup",
                "first_admin_login_seen": False,
                "bootstrap_completed_at": None,
            }
        ),
        encoding="utf-8",
    )

    from hermes_cli.product_setup import _print_product_setup_summary

    _print_product_setup_summary()

    out = capsys.readouterr().out
    assert "Tailnet app URL:         https://hermes-box.corpnet.ts.net" in out
    assert "Tailnet auth URL:        https://hermes-box.corpnet.ts.net:4444" in out
    assert "Tailnet access:         available after local admin bootstrap" in out
    assert "Pocket ID setup is intentionally local-only." in out
    assert "Complete bootstrap at: http://localhost:1411/setup" in out


def test_product_setup_summary_shows_tailnet_urls_after_bootstrap_completion(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.product_config import save_product_config

    product_config = load_product_config()
    product_config["network"]["tailscale"]["enabled"] = True
    product_config["network"]["tailscale"]["tailnet_name"] = "corpnet"
    product_config["network"]["tailscale"]["device_name"] = "hermes-box"
    product_config["network"]["public_host"] = "localhost"
    save_product_config(product_config)
    state_path = tmp_path / "product" / "bootstrap" / "first_admin_enrollment.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "bootstrap_mode": "native_setup",
                "setup_url": "http://localhost:1411/setup",
                "first_admin_login_seen": True,
                "bootstrap_completed_at": 1710000000,
            }
        ),
        encoding="utf-8",
    )
    activation_path = tmp_path / "product" / "bootstrap" / "tailnet_activation.json"
    activation_path.write_text(
        json.dumps({"status": "active", "activated_at": 1710000000}),
        encoding="utf-8",
    )

    from hermes_cli.product_setup import _print_product_setup_summary

    _print_product_setup_summary()

    out = capsys.readouterr().out
    assert "Current app URL:         https://hermes-box.corpnet.ts.net" in out
    assert "Current Pocket ID URL:   https://hermes-box.corpnet.ts.net:4444" in out
    assert "Tailnet app URL:         https://hermes-box.corpnet.ts.net" in out
    assert "Tailnet auth URL:        https://hermes-box.corpnet.ts.net:4444" in out
    assert "Tailnet access:         active" in out
    assert "pending first admin bootstrap" not in out


def test_product_setup_summary_shows_lan_urls_when_bind_host_all_interfaces(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.product_config import save_product_config

    product_config = load_product_config()
    product_config["network"]["bind_host"] = "0.0.0.0"
    product_config["network"]["app_port"] = 18086
    product_config["network"]["pocket_id_port"] = 19111
    save_product_config(product_config)

    from hermes_cli.product_setup import _print_product_setup_summary

    _print_product_setup_summary()
    out = capsys.readouterr().out
    assert "Service bind host:       0.0.0.0 (LAN reachable)" in out
    assert "LAN app URL:             http://<HOST_IP>:18086" in out
    assert "LAN auth URL:            http://<HOST_IP>:19111" in out


def test_product_setup_summary_marks_lan_disabled_for_loopback_bind_host(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.product_config import save_product_config

    product_config = load_product_config()
    product_config["network"]["bind_host"] = "127.0.0.1"
    save_product_config(product_config)

    from hermes_cli.product_setup import _print_product_setup_summary

    _print_product_setup_summary()
    out = capsys.readouterr().out
    assert "Service bind host:       127.0.0.1 (local-only)" in out
    assert "LAN access URL:          disabled (set network.bind_host to 0.0.0.0)" in out


def test_product_setup_prints_install_handoff_when_started_from_install(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.setup_product_network"),
        patch("hermes_cli.product_setup.setup_product_tailscale"),
        patch("hermes_cli.product_setup.setup_product_identity"),
        patch("hermes_cli.product_setup.setup_product_storage"),
        patch("hermes_cli.product_setup._run_bootstrap_section"),
        patch("hermes_cli.product_setup._print_product_setup_summary"),
        patch("hermes_cli.product_setup.print_success"),
        patch("hermes_cli.product_setup._clear_terminal_screen"),
    ):
        run_product_setup_wizard(_make_product_args(from_install=True))

    out = capsys.readouterr().out
    assert "Host prerequisites are ready." in out
    assert "Starting the product setup wizard..." in out


def test_product_setup_bootstrap_section_validates_host_prereqs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.validate_product_host_prereqs") as mock_validate,
        patch("hermes_cli.product_setup._validate_product_ports_available") as mock_ports,
        patch("hermes_cli.product_setup.initialize_product_stack"),
        patch("hermes_cli.product_setup._start_product_stack"),
    ):
        run_product_setup_wizard(_make_product_args(section="bootstrap"))

    mock_validate.assert_called_once()
    mock_ports.assert_called_once()


def test_product_setup_bootstrap_section_exits_cleanly_on_prereq_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.validate_product_host_prereqs", side_effect=RuntimeError("Docker is not available")),
    ):
        with patch("hermes_cli.product_setup.initialize_product_stack") as mock_init:
            with patch("hermes_cli.product_setup._start_product_stack") as mock_start:
                with pytest.raises(SystemExit, match="Docker is not available"):
                    run_product_setup_wizard(_make_product_args(section="bootstrap"))

    mock_init.assert_not_called()
    mock_start.assert_not_called()


def test_product_setup_bootstrap_section_exits_cleanly_on_port_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with (
        patch("hermes_cli.product_setup.is_interactive_stdin", return_value=True),
        patch("hermes_cli.product_setup.validate_product_host_prereqs"),
        patch(
            "hermes_cli.product_setup._validate_product_ports_available",
            side_effect=RuntimeError("Pocket ID port 1411 on 0.0.0.0 is already in use."),
        ),
        patch("hermes_cli.product_setup.initialize_product_stack") as mock_init,
        patch("hermes_cli.product_setup._start_product_stack") as mock_start,
    ):
        with pytest.raises(SystemExit, match="Pocket ID port 1411"):
            run_product_setup_wizard(_make_product_args(section="bootstrap"))

    mock_init.assert_not_called()
    mock_start.assert_not_called()
