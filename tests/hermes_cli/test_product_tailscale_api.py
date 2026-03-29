import json

import httpx

from hermes_cli.product_config import load_product_config
from hermes_cli.product_tailscale_api import (
    ensure_tsidp_policy,
    get_tailscale_policy_backups_root,
    merge_tsidp_policy_grants,
    policy_has_required_tsidp_grants,
)


def _config():
    config = load_product_config()
    config["network"]["tailscale"]["enabled"] = True
    config["network"]["tailscale"]["tailnet_name"] = "tail5fd7a5"
    config["network"]["tailscale"]["api_tailnet_name"] = "example.github"
    return config


def test_merge_tsidp_policy_grants_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    policy = {"grants": [{"src": ["*"], "dst": ["*"], "ip": ["*"]}], "ssh": [{"action": "check"}]}

    merged, changed = merge_tsidp_policy_grants(policy)
    merged_again, changed_again = merge_tsidp_policy_grants(merged)

    assert changed is True
    assert policy_has_required_tsidp_grants(merged) is True
    assert merged["ssh"] == [{"action": "check"}]
    assert changed_again is False
    assert merged_again == merged


def test_ensure_tsidp_policy_updates_policy_and_creates_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PRODUCT_TAILSCALE_API_TOKEN", "tskey-api-kv")
    calls = []
    current_policy = {"grants": [{"src": ["*"], "dst": ["*"], "ip": ["*"]}]}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and len(calls) == 1:
            return httpx.Response(200, json=current_policy)
        if request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            assert policy_has_required_tsidp_grants(payload) is True
            return httpx.Response(200, json=payload)
        if request.method == "GET":
            merged, _ = merge_tsidp_policy_grants(current_policy)
            return httpx.Response(200, json=merged)
        raise AssertionError("unexpected request")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def make_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("hermes_cli.product_tailscale_api.httpx.Client", make_client)

    result = ensure_tsidp_policy(_config())

    assert result["changed"] is True
    assert result["tailnet"] == "example.github"
    assert get_tailscale_policy_backups_root().exists()
    assert result["backup_path"]
    assert calls == [
        ("GET", "/api/v2/tailnet/example.github/acl"),
        ("POST", "/api/v2/tailnet/example.github/acl"),
        ("GET", "/api/v2/tailnet/example.github/acl"),
    ]
