from argparse import Namespace

import pytest

from hermes_cli.product_install import (
    DOCKER_GROUP_RELOGIN_EXIT_CODE,
    PRODUCT_APP_SERVICE_NAME,
    _render_product_app_service_unit,
    ensure_product_app_service_started,
    run_product_install,
)


def test_render_product_app_service_unit_targets_only_product_app(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CORE_INSTALL_DIR", str(tmp_path / "checkout"))
    monkeypatch.setattr("hermes_cli.product_install._product_service_identity", lambda: ("alice", "/home/alice"))

    rendered = _render_product_app_service_unit({"network": {"app_port": 18086}})

    assert "create_product_app" in rendered
    assert "create_product_auth_proxy_app" not in rendered
    assert "--port 18086" in rendered


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
