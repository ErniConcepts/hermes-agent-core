from hermes_cli.product_runtime_network import (
    PRODUCT_RUNTIME_NETWORK_GATEWAY,
    PRODUCT_RUNTIME_NETWORK_NAME,
    PRODUCT_RUNTIME_NETWORK_SUBNET,
    ensure_runtime_docker_network,
    host_default_route_mtu,
    local_host_model_port,
    runtime_network_spec,
)


def test_runtime_network_spec_is_fixed():
    assert runtime_network_spec() == {
        "name": PRODUCT_RUNTIME_NETWORK_NAME,
        "subnet": PRODUCT_RUNTIME_NETWORK_SUBNET,
        "gateway": PRODUCT_RUNTIME_NETWORK_GATEWAY,
    }


def test_local_host_model_port_detects_loopback_model():
    assert local_host_model_port({"model": {"base_url": "http://127.0.0.1:8080/v1"}}) == 8080


def test_local_host_model_port_ignores_remote_model():
    assert local_host_model_port({"model": {"base_url": "https://api.openai.com/v1"}}) is None


def test_local_host_model_port_ignores_legacy_non_mapping_model_config():
    assert local_host_model_port({"model": "qwen3.5-9b-local"}) is None


def test_ensure_runtime_docker_network_creates_missing_network():
    calls = []

    def _run(command, **kwargs):
        calls.append(command)
        if command[:4] == ["docker", "network", "inspect", PRODUCT_RUNTIME_NETWORK_NAME]:
            return type("_Result", (), {"returncode": 1})()
        return type("_Result", (), {"returncode": 0})()

    changed = ensure_runtime_docker_network(_run)

    assert changed is True
    assert calls[1] == [
        "docker",
        "network",
        "create",
        "--driver",
        "bridge",
        "--subnet",
        PRODUCT_RUNTIME_NETWORK_SUBNET,
        "--gateway",
        PRODUCT_RUNTIME_NETWORK_GATEWAY,
        PRODUCT_RUNTIME_NETWORK_NAME,
    ]


def test_ensure_runtime_docker_network_includes_host_mtu(monkeypatch):
    calls = []

    def _run(command, **kwargs):
        calls.append(command)
        if command[:4] == ["docker", "network", "inspect", PRODUCT_RUNTIME_NETWORK_NAME]:
            return type("_Result", (), {"returncode": 1})()
        return type("_Result", (), {"returncode": 0})()

    monkeypatch.setattr("hermes_cli.product_runtime_network.host_default_route_mtu", lambda: 1360)

    changed = ensure_runtime_docker_network(_run)

    assert changed is True
    assert calls[1] == [
        "docker",
        "network",
        "create",
        "--driver",
        "bridge",
        "--subnet",
        PRODUCT_RUNTIME_NETWORK_SUBNET,
        "--gateway",
        PRODUCT_RUNTIME_NETWORK_GATEWAY,
        "--opt",
        "com.docker.network.driver.mtu=1360",
        PRODUCT_RUNTIME_NETWORK_NAME,
    ]


def test_host_default_route_mtu_returns_none_when_ip_missing(monkeypatch):
    monkeypatch.setattr("hermes_cli.product_runtime_network.shutil.which", lambda name: None)

    assert host_default_route_mtu() is None
