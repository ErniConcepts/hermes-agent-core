from argparse import Namespace
import subprocess

import pytest
import hermes_cli.product_install as product_install

from hermes_cli.product_install import (
    APT_INSTALL_PACKAGES,
    PRODUCT_APP_SERVICE_NAME,
    PRODUCT_RUNTIME_IMAGE_TAG,
    RUNSC_RUNTIME_CONFIG,
    _render_product_app_service_unit,
    _linux_distro_id,
    _runsc_runtime_matches,
    build_product_runtime_image,
    ensure_linux_product_host_prereqs,
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


def test_ensure_linux_product_host_prereqs_installs_apt_packages_and_adds_group(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._apt_supported_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._docker_available", lambda: False)
    monkeypatch.setattr("hermes_cli.product_install._docker_compose_available", lambda: False)
    monkeypatch.setattr("hermes_cli.product_install._runsc_available", lambda: False)
    monkeypatch.setattr("hermes_cli.product_install._current_user_name", lambda: "alice")
    monkeypatch.setattr("hermes_cli.product_install._user_in_group", lambda group, user=None: False)
    calls = []
    monkeypatch.setattr("hermes_cli.product_install._apt_install", lambda packages: calls.append(("apt", packages)))
    monkeypatch.setattr("hermes_cli.product_install._start_and_enable_docker_service", lambda: calls.append(("docker", None)))
    monkeypatch.setattr("hermes_cli.product_install._run", lambda command, **kwargs: calls.append(("run", command)))

    result = ensure_linux_product_host_prereqs()

    assert result == {"installed_packages": True, "added_docker_group_membership": True}
    assert ("apt", APT_INSTALL_PACKAGES) in calls
    assert ("docker", None) in calls
    assert ("run", ["usermod", "-aG", "docker", "alice"]) in calls


def test_ensure_linux_product_host_prereqs_rejects_unsupported_distro(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._apt_supported_linux", lambda: False)

    with pytest.raises(RuntimeError, match="Ubuntu/Debian"):
        ensure_linux_product_host_prereqs()


def test_run_wraps_noninteractive_sudo_failure_with_product_message(monkeypatch):
    import hermes_cli.product_install as product_install

    _run = product_install._run

    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr(product_install.os, "geteuid", lambda: 1000, raising=False)

    def _boom(*args, **kwargs):
        raise subprocess.CalledProcessError(
            1,
            args[0],
            stderr="sudo: a password is required",
        )

    monkeypatch.setattr("hermes_cli.product_install.subprocess.run", _boom)

    with pytest.raises(RuntimeError, match="interactive local shell"):
        _run(["apt-get", "update"], sudo=True)


def test_linux_distro_id_reads_uppercase_os_release_keys(tmp_path, monkeypatch):
    os_release = tmp_path / "os-release"
    os_release.write_text('NAME="Ubuntu"\nID=ubuntu\n', encoding="utf-8")

    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install.Path", lambda value: os_release)

    assert _linux_distro_id() == "ubuntu"


def test_runsc_runtime_matches_accepts_absolute_binary_path():
    assert _runsc_runtime_matches({"path": "/usr/bin/runsc", "runtimeArgs": ["--network=host"]}) is True
    assert _runsc_runtime_matches({"path": "runsc", "runtimeArgs": ["--network=host"]}) is True
    assert _runsc_runtime_matches({"path": "/usr/bin/runsc", "runtimeArgs": []}) is False


def test_run_product_install_records_state_and_runs_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.product_install.ensure_linux_product_host_prereqs",
        lambda: {"installed_packages": False, "added_docker_group_membership": False},
    )
    monkeypatch.setattr("hermes_cli.product_install._docker_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._docker_compose_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._runsc_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install.ensure_runsc_registered_with_docker", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install.build_product_runtime_image", lambda: None)
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


def test_run_product_install_skip_setup_does_not_start_product_app_service(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.product_install.ensure_linux_product_host_prereqs",
        lambda: {"installed_packages": False, "added_docker_group_membership": False},
    )
    monkeypatch.setattr("hermes_cli.product_install._docker_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._docker_compose_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._runsc_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install.ensure_runsc_registered_with_docker", lambda: False)
    monkeypatch.setattr("hermes_cli.product_install.build_product_runtime_image", lambda: None)
    seen = {}
    monkeypatch.setattr(
        "hermes_cli.product_install.ensure_product_app_service_started",
        lambda config=None: seen.setdefault("service_started", True),
    )

    run_product_install(Namespace(skip_setup=True, non_interactive=True, section=None))

    assert "service_started" not in seen


def test_run_product_install_prompts_for_newgrp_before_generic_docker_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.product_install.ensure_linux_product_host_prereqs",
        lambda: {"installed_packages": True, "added_docker_group_membership": True},
    )
    monkeypatch.setattr("hermes_cli.product_install._docker_available", lambda: False)

    with pytest.raises(SystemExit, match="newgrp docker"):
        run_product_install(Namespace(skip_setup=True, non_interactive=True, section=None))


def test_run_product_install_repairs_runsc_registration_before_docker_health_check(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.product_install.ensure_linux_product_host_prereqs",
        lambda: {"installed_packages": True, "added_docker_group_membership": False},
    )
    monkeypatch.setattr("hermes_cli.product_install._runsc_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._docker_compose_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install.build_product_runtime_image", lambda: None)
    seen = []
    monkeypatch.setattr(
        "hermes_cli.product_install.ensure_runsc_registered_with_docker",
        lambda: seen.append("runsc") or False,
    )
    monkeypatch.setattr("hermes_cli.product_install._docker_available", lambda: seen.append("docker") or False)

    with pytest.raises(SystemExit, match="Docker is not available"):
        run_product_install(Namespace(skip_setup=True, non_interactive=True, section=None))

    assert seen == ["runsc", "docker"]


def test_render_product_app_service_unit_uses_non_root_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.product_install._product_service_identity", lambda: ("alice", "/home/alice"))

    rendered = _render_product_app_service_unit({"network": {"app_port": 18086}})

    assert "User=alice" not in rendered
    assert "WorkingDirectory=/home/alice" in rendered
    assert "Environment=HOME=/home/alice" in rendered
    assert f"Environment=HERMES_HOME={tmp_path}" in rendered
    assert "--port 18086" in rendered
    assert "WantedBy=default.target" in rendered


def test_ensure_product_app_service_started_installs_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.product_install._is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._systemd_available", lambda: True)
    monkeypatch.setattr("hermes_cli.product_install._product_service_identity", lambda: ("alice", "/home/alice"))
    monkeypatch.setattr("hermes_cli.product_install.Path.home", lambda: tmp_path)
    calls = []
    responses = iter([1])

    def _fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["systemctl", "--user", "is-active"]:
            return type("_Result", (), {"returncode": next(responses)})()
        return type("_Result", (), {"returncode": 0})()

    monkeypatch.setattr("hermes_cli.product_install._run", _fake_run)

    ensure_product_app_service_started({"network": {"app_port": 8086}})

    service_path = tmp_path / ".config" / "systemd" / "user" / PRODUCT_APP_SERVICE_NAME
    assert service_path.exists()
    assert calls[0][0] == ["systemctl", "--user", "daemon-reload"]
    assert calls[1][0] == ["systemctl", "--user", "enable", PRODUCT_APP_SERVICE_NAME]
    assert calls[2][0] == ["systemctl", "--user", "is-active", PRODUCT_APP_SERVICE_NAME]
    assert calls[3][0] == ["systemctl", "--user", "start", PRODUCT_APP_SERVICE_NAME]


def test_start_and_enable_docker_service_starts_socket_and_service(monkeypatch):
    from hermes_cli.product_install import _start_and_enable_docker_service

    monkeypatch.setattr("hermes_cli.product_install._systemd_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        "hermes_cli.product_install._run",
        lambda command, **kwargs: calls.append(command) or type("_Result", (), {"returncode": 0})(),
    )

    _start_and_enable_docker_service()

    assert calls == [
        ["systemctl", "enable", "--now", "docker.socket"],
        ["systemctl", "enable", "--now", "docker"],
    ]


def test_restart_docker_service_resets_failed_socket_state(monkeypatch):
    from hermes_cli.product_install import _restart_docker_service

    monkeypatch.setattr("hermes_cli.product_install._systemd_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        "hermes_cli.product_install._run",
        lambda command, **kwargs: calls.append((command, kwargs)) or type("_Result", (), {"returncode": 0})(),
    )

    _restart_docker_service()

    assert calls == [
        (["systemctl", "stop", "docker", "docker.socket"], {"check": False, "sudo": True}),
        (["systemctl", "reset-failed", "docker", "docker.socket"], {"check": False, "sudo": True}),
        (["systemctl", "start", "docker.socket"], {"sudo": True}),
        (["systemctl", "start", "docker"], {"sudo": True}),
    ]


def test_build_product_runtime_image_uses_local_checkout(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "hermes_cli.product_install._run",
        lambda command, **kwargs: calls.append((command, kwargs)) or type("_Result", (), {"returncode": 0})(),
    )

    build_product_runtime_image()

    assert calls == [
        (
            [
                "docker",
                "build",
                "-t",
                PRODUCT_RUNTIME_IMAGE_TAG,
                "-f",
                str(product_install.PRODUCT_RUNTIME_DOCKERFILE),
                str(product_install.PROJECT_ROOT),
            ],
            {"capture_output": False},
        )
    ]


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
