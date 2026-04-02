from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hermes_cli.config import load_config

PRODUCT_RUNTIME_NETWORK_NAME = "hermes-product-runtime"
PRODUCT_RUNTIME_NETWORK_SUBNET = "172.31.240.0/24"
PRODUCT_RUNTIME_NETWORK_GATEWAY = "172.31.240.1"
_FIREWALL_COMMENT_PREFIX = "hermes-product-runtime"


def runtime_network_name() -> str:
    return PRODUCT_RUNTIME_NETWORK_NAME


def runtime_network_spec() -> dict[str, str]:
    return {
        "name": PRODUCT_RUNTIME_NETWORK_NAME,
        "subnet": PRODUCT_RUNTIME_NETWORK_SUBNET,
        "gateway": PRODUCT_RUNTIME_NETWORK_GATEWAY,
    }


def docker_network_exists(run_fn: Any, name: str) -> bool:
    result = run_fn(["docker", "network", "inspect", name], check=False)
    return result.returncode == 0


def ensure_runtime_docker_network(run_fn: Any) -> bool:
    spec = runtime_network_spec()
    if docker_network_exists(run_fn, spec["name"]):
        return False
    command = [
        "docker",
        "network",
        "create",
        "--driver",
        "bridge",
        "--subnet",
        spec["subnet"],
        "--gateway",
        spec["gateway"],
    ]
    mtu = host_default_route_mtu()
    if mtu is not None:
        command.extend(["--opt", f"com.docker.network.driver.mtu={mtu}"])
    command.append(spec["name"])
    run_fn(command)
    return True


def remove_runtime_docker_network(run_fn: Any) -> bool:
    spec = runtime_network_spec()
    if not docker_network_exists(run_fn, spec["name"]):
        return False
    run_fn(["docker", "network", "rm", spec["name"]], check=False)
    return True


def local_host_model_port(config: dict[str, Any] | None = None) -> int | None:
    root_config = config or load_config()
    model_value = root_config.get("model") if isinstance(root_config, dict) else None
    model = model_value if isinstance(model_value, dict) else {}
    base_url = str(model.get("base_url") or "").strip()
    if not base_url:
        return None
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    hostname = (parsed.hostname or "").strip().lower()
    if hostname not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        return None
    if parsed.port is not None:
        return int(parsed.port)
    return 443 if parsed.scheme == "https" else 80


def _iptables_available() -> bool:
    return shutil.which("iptables") is not None


def host_default_route_mtu() -> int | None:
    ip = shutil.which("ip")
    if not ip:
        return None
    try:
        route_result = subprocess.run(
            [ip, "route", "show", "default"],
            capture_output=True,
            text=True,
            check=True,
        )
        route_line = next((line.strip() for line in route_result.stdout.splitlines() if line.strip()), "")
        if not route_line:
            return None
        parts = route_line.split()
        if "dev" not in parts:
            return None
        interface = parts[parts.index("dev") + 1]
        link_result = subprocess.run(
            [ip, "link", "show", "dev", interface],
            capture_output=True,
            text=True,
            check=True,
        )
        link_tokens = link_result.stdout.replace("\n", " ").split()
        for token_index, token in enumerate(link_tokens):
            if token == "mtu" and token_index + 1 < len(link_tokens):
                try:
                    return int(link_tokens[token_index + 1])
                except ValueError:
                    return None
    except Exception:
        return None
    return None


def _ensure_docker_user_rule(run_fn: Any, args: list[str]) -> bool:
    check_cmd = ["iptables", "-C", "DOCKER-USER", *args]
    if run_fn(check_cmd, check=False, sudo=True).returncode == 0:
        return False
    run_fn(["iptables", "-I", "DOCKER-USER", *args], sudo=True)
    return True


def ensure_runtime_host_firewall(run_fn: Any, *, model_port: int | None) -> bool:
    if not _iptables_available():
        raise RuntimeError("iptables is required for Hermes Core runtime host firewall rules")
    spec = runtime_network_spec()
    changed = False
    changed = _ensure_docker_user_rule(
        run_fn,
        [
            "-s",
            spec["subnet"],
            "-m",
            "conntrack",
            "--ctstate",
            "RELATED,ESTABLISHED",
            "-m",
            "comment",
            "--comment",
            f"{_FIREWALL_COMMENT_PREFIX}-established",
            "-j",
            "RETURN",
        ],
    ) or changed
    if model_port is not None:
        changed = _ensure_docker_user_rule(
            run_fn,
            [
                "-s",
                spec["subnet"],
                "-d",
                spec["gateway"],
                "-p",
                "tcp",
                "--dport",
                str(int(model_port)),
                "-m",
                "comment",
                "--comment",
                f"{_FIREWALL_COMMENT_PREFIX}-model",
                "-j",
                "RETURN",
            ],
        ) or changed
    changed = _ensure_docker_user_rule(
        run_fn,
        [
            "-s",
            spec["subnet"],
            "-m",
            "addrtype",
            "--dst-type",
            "LOCAL",
            "-m",
            "comment",
            "--comment",
            f"{_FIREWALL_COMMENT_PREFIX}-drop-local",
            "-j",
            "DROP",
        ],
    ) or changed
    return changed


def remove_runtime_host_firewall(run_fn: Any, *, model_port: int | None) -> bool:
    if not _iptables_available():
        return False
    spec = runtime_network_spec()
    rules: list[list[str]] = [
        [
            "-s",
            spec["subnet"],
            "-m",
            "conntrack",
            "--ctstate",
            "RELATED,ESTABLISHED",
            "-m",
            "comment",
            "--comment",
            f"{_FIREWALL_COMMENT_PREFIX}-established",
            "-j",
            "RETURN",
        ],
        [
            "-s",
            spec["subnet"],
            "-m",
            "addrtype",
            "--dst-type",
            "LOCAL",
            "-m",
            "comment",
            "--comment",
            f"{_FIREWALL_COMMENT_PREFIX}-drop-local",
            "-j",
            "DROP",
        ],
    ]
    if model_port is not None:
        rules.append(
            [
                "-s",
                spec["subnet"],
                "-d",
                spec["gateway"],
                "-p",
                "tcp",
                "--dport",
                str(int(model_port)),
                "-m",
                "comment",
                "--comment",
                f"{_FIREWALL_COMMENT_PREFIX}-model",
                "-j",
                "RETURN",
            ]
        )
    changed = False
    for rule in rules:
        if run_fn(["iptables", "-C", "DOCKER-USER", *rule], check=False, sudo=True).returncode == 0:
            run_fn(["iptables", "-D", "DOCKER-USER", *rule], check=False, sudo=True)
            changed = True
    return changed

