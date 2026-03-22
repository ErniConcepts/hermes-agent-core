from argparse import Namespace

import pytest

from hermes_cli.product_install import (
    PRODUCT_APP_SERVICE_NAME,
    RUNSC_RUNTIME_CONFIG,
    _render_product_app_service_unit,
    ensure_product_app_service_started,
    ensure_runsc_registered_with_docker,
    get_product_install_state_path,
    perform_product_cleanup,
    run_product_install,
    save_product_install_state,
    validate_product_host_prereqs,
    _remove_runsc_registration_if_managed,
)


def test_ensure_runsc_registered_with_docker_updates_daemon_config(monkeypatch):
    written = {}

    monkeypatch.setattr(
        "hermes_cli.product_install._load_docker_daemon_config",
        lambda: ({}, False),
    )
    monkeypatch.setattr(
        "hermes_cli.product_install._write_docker_daemon_config",
        lambda config: written.setdefault("config", config),
    )
    monkeypatch.setattr("hermes_cli.product_install._restart_docker_service", lambda: None)
    monkeypatch.setattr("hermes_cli.product_install._runsc_registered", lambda: True)

    changed = ensure_runsc_registered_with_docker()

    assert changed is True
    assert written["config"]["runtimes"]["runsc"] == RUNSC_RUNTIME_CONFIG


def test_validate_product_host_prereqs_requires_registered_runsc(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._docker_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._docker_compose_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._runsc_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._runsc_registered", lambda: False)

    with pytest.raises(RuntimeError, match="runsc"):
        validate_product_host_prereqs()


def test_run_product_install_records_state_and_runs_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._docker_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._docker_compose_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._runsc_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install.ensure_runsc_registered_with_docker", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install.ensure_product_app_service_started", lambda config=None: None)
    monkeypatch.setattr("hermes_cli.product_install.validate_product_host_prereqs", lambda: None)

    invoked = {}
    monkeypatch.setattr(
        "hermes_cli.product_setup.run_product_setup_wizard",
        lambda args: invoked.setdefault("args", args),
    )

    run_product_install(Namespace(skip_setup=False, non_interactive=True, section=None))

    state = get_product_install_state_path().read_text(encoding="utf-8")
    assert '"managed_runsc_registration": true' in state
    assert invoked["args"].non_interactive is True


def test_render_product_app_service_unit_uses_non_root_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_install._product_service_identity", lambda: ("alice", "/home/alice"))

    rendered = _render_product_app_service_unit({"network": {"app_port": 18086}})

    assert "User=alice" in rendered
    assert "Environment=HOME=/home/alice" in rendered
    assert "Environment=HERMES_HOME=/home/alice/.hermes" in rendered
    assert "--port 18086" in rendered


def test_ensure_product_app_service_started_installs_and_restarts(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._systemd_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._write_product_app_service_unit", lambda config=None: None)
    calls = []
    responses = iter([1])

    def _fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ["systemctl", "is-active"]:
            return type("_Result", (), {"returncode": next(responses)})()
        return type("_Result", (), {"returncode": 0})()

    monkeypatch.setattr("hermes_cli.product_install._run", _fake_run)

    ensure_product_app_service_started({"network": {"app_port": 8086}})

    assert calls[0][0] == ["systemctl", "daemon-reload"]
    assert calls[1][0] == ["systemctl", "enable", PRODUCT_APP_SERVICE_NAME]
    assert calls[2][0] == ["systemctl", "is-active", PRODUCT_APP_SERVICE_NAME]
    assert calls[3][0] == ["systemctl", "start", PRODUCT_APP_SERVICE_NAME]


def test_perform_product_cleanup_removes_product_files_and_env_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    product_root = tmp_path / "product"
    product_root.mkdir(parents=True)
    (product_root / "services").mkdir()
    (tmp_path / "product.yaml").write_text("product: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "HERMES_PRODUCT_OIDC_CLIENT_SECRET=one\n"
        "HERMES_POCKET_ID_STATIC_API_KEY=two\n"
        "HERMES_POCKET_ID_ENCRYPTION_KEY=three\n"
        "OTHER_KEY=keep\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("hermes_cli.product_install._docker_available", lambda: False)
    monkeypatch.setattr("hermes_cli.product_install._remove_runsc_registration_if_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: False)

    result = perform_product_cleanup()

    assert result["removed_runsc_registration"] is False
    assert not product_root.exists()
    assert not (tmp_path / "product.yaml").exists()
    assert (tmp_path / ".env").read_text(encoding="utf-8").strip() == "OTHER_KEY=keep"


def test_remove_runsc_registration_if_managed_updates_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    save_product_install_state({"managed_runsc_registration": True})
    written = {}

    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.product_install._load_docker_daemon_config",
        lambda: ({"runtimes": {"runsc": dict(RUNSC_RUNTIME_CONFIG), "runc": {"path": "runc"}}}, True),
    )
    monkeypatch.setattr(
        "hermes_cli.product_install._write_docker_daemon_config",
        lambda config: written.setdefault("config", config),
    )
    monkeypatch.setattr("hermes_cli.product_install._restart_docker_service", lambda: None)

    changed = _remove_runsc_registration_if_managed()

    assert changed is True
    assert "runsc" not in written["config"]["runtimes"]
    assert "runc" in written["config"]["runtimes"]
    assert '"managed_runsc_registration": false' in get_product_install_state_path().read_text(encoding="utf-8")
