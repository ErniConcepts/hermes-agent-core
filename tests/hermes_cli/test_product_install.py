import shlex
from argparse import Namespace

import pytest

from hermes_cli.product_install import (
    DOCKER_GROUP_RELOGIN_EXIT_CODE,
    PRODUCT_APP_SERVICE_NAME,
    RUNSC_RUNTIME_CONFIG,
    _render_product_app_service_unit,
    ensure_product_runtime_networking,
    ensure_product_app_service_started,
    run_product_install,
)
from hermes_cli.product_install_host import runsc_runtime_matches


def test_render_product_app_service_unit_targets_only_product_app(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CORE_INSTALL_DIR", str(tmp_path / "checkout"))
    monkeypatch.setattr("hermes_cli.product_install._product_service_identity", lambda: ("alice", "/home/alice"))

    rendered = _render_product_app_service_unit({"network": {"app_port": 18086}})

    assert "create_product_app" in rendered
    assert "create_product_auth_proxy_app" not in rendered
    assert "--port 18086" in rendered


def test_render_product_app_service_unit_shell_quotes_bind_host(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CORE_INSTALL_DIR", str(tmp_path / "checkout"))
    monkeypatch.setattr("hermes_cli.product_install._product_service_identity", lambda: ("alice", "/home/alice"))
    bind_host = "0.0.0.0'; echo pwned; echo '"

    rendered = _render_product_app_service_unit({"network": {"bind_host": bind_host, "app_port": 18086}})

    assert shlex.quote(bind_host) in rendered
    assert "--host" in rendered


def test_render_product_app_service_unit_defaults_to_localhost(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CORE_INSTALL_DIR", str(tmp_path / "checkout"))
    monkeypatch.setattr("hermes_cli.product_install._product_service_identity", lambda: ("alice", "/home/alice"))

    rendered = _render_product_app_service_unit({"network": {"app_port": 18086}})

    assert "--host 127.0.0.1" in rendered


def test_render_product_app_service_unit_rejects_invalid_port(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CORE_INSTALL_DIR", str(tmp_path / "checkout"))
    monkeypatch.setattr("hermes_cli.product_install._product_service_identity", lambda: ("alice", "/home/alice"))

    with pytest.raises(ValueError):
        _render_product_app_service_unit({"network": {"app_port": 70000}})


def test_ensure_product_app_service_started_manages_only_one_unit(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._systemd_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._product_service_identity", lambda: ("alice", "/home/alice"))
    monkeypatch.setattr("hermes_cli.product_install.Path.home", lambda: tmp_path)
    calls = []

    def _fake_run(command, **kwargs):
        calls.append(command)
        return type("_Result", (), {"returncode": 1 if command[:3] == ["systemctl", "--user", "is-active"] else 0})()

    monkeypatch.setattr("hermes_cli.product_install._run", _fake_run)

    ensure_product_app_service_started({"network": {"app_port": 8086}})

    assert (tmp_path / ".config" / "systemd" / "user" / PRODUCT_APP_SERVICE_NAME).exists()
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", PRODUCT_APP_SERVICE_NAME],
        ["systemctl", "--user", "is-active", PRODUCT_APP_SERVICE_NAME],
        ["systemctl", "--user", "start", PRODUCT_APP_SERVICE_NAME],
    ]


def test_run_product_install_requests_relogin_after_docker_group_change(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.product_install.ensure_linux_product_host_prereqs",
        lambda: {"installed_packages": True, "added_docker_group_membership": True},
    )
    monkeypatch.setattr("hermes_cli.product_install._docker_readiness_probe", lambda: (False, "Docker shell not ready"))

    with pytest.raises(SystemExit) as excinfo:
        run_product_install(Namespace(skip_setup=True, non_interactive=True, section=None))

    assert excinfo.value.code == DOCKER_GROUP_RELOGIN_EXIT_CODE


def test_runsc_runtime_config_does_not_use_host_network():
    assert RUNSC_RUNTIME_CONFIG["runtimeArgs"] == []


def test_runsc_runtime_matches_accepts_missing_empty_runtime_args():
    assert runsc_runtime_matches("runsc", RUNSC_RUNTIME_CONFIG, {"path": "runsc"}) is True


def test_ensure_product_runtime_networking_uses_bridge_network_and_firewall(monkeypatch):
    seen = {}
    monkeypatch.setattr("hermes_cli.product_install.ensure_runtime_docker_network", lambda run_fn: seen.setdefault("network", True))
    monkeypatch.setattr("hermes_cli.product_install.ensure_runtime_host_firewall", lambda run_fn, model_port=None: seen.setdefault("firewall_port", model_port) == model_port)
    monkeypatch.setattr("hermes_cli.product_install.local_host_model_port", lambda config=None: 8080)

    result = ensure_product_runtime_networking()

    assert result == {"created_network": True, "updated_firewall": True}
    assert seen["firewall_port"] == 8080
