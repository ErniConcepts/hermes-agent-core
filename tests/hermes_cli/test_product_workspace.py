import pytest

from hermes_cli.product_workspace import (
    ProductWorkspaceQuotaError,
    create_workspace_folder,
    get_workspace_state,
    humanize_bytes,
    store_workspace_file,
)


def _user():
    return {"sub": "user-1", "preferred_username": "alice", "name": "Alice"}


def test_workspace_state_defaults_to_empty_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    state = get_workspace_state(_user())

    assert state.current_path == ""
    assert state.entries == []
    assert state.used_bytes == 0
    assert state.limit_bytes == 2048 * 1024 * 1024


def test_workspace_can_create_folder_and_list_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    create_workspace_folder(_user(), parent_path="", folder_name="reports")
    state = get_workspace_state(_user())

    assert [entry.name for entry in state.entries] == ["reports"]
    assert state.entries[0].kind == "folder"


def test_workspace_can_store_file_in_nested_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    create_workspace_folder(_user(), parent_path="", folder_name="reports")
    state = store_workspace_file(
        _user(),
        parent_path="reports",
        filename="budget.txt",
        content=b"budget",
    )

    assert state.current_path == "reports"
    assert [entry.name for entry in state.entries] == ["budget.txt"]
    assert state.entries[0].size_bytes == 6


def test_workspace_rejects_uploads_over_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def _config():
        return {
            "storage": {
                "root": "product",
                "users_root": "product/users",
                "user_workspace_limit_mb": 1,
            }
        }

    with monkeypatch.context() as context:
        context.setattr("hermes_cli.product_workspace.load_product_config", _config)
        try:
            store_workspace_file(
                _user(),
                parent_path="",
                filename="too-big.bin",
                content=b"x" * (2 * 1024 * 1024),
            )
        except ProductWorkspaceQuotaError as exc:
            assert "Workspace storage limit exceeded" in str(exc)
        else:
            raise AssertionError("Expected quota error")


def test_humanize_bytes_formats_small_and_large_values():
    assert humanize_bytes(999) == "999 B"
    assert humanize_bytes(1024 * 1024) == "1 MB"


def test_workspace_rejects_invalid_quota_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def _config():
        return {
            "storage": {
                "root": "product",
                "users_root": "product/users",
                "user_workspace_limit_mb": "bad-value",
            }
        }

    with monkeypatch.context() as context:
        context.setattr("hermes_cli.product_workspace.load_product_config", _config)
        with pytest.raises(ValueError, match="user_workspace_limit_mb"):
            get_workspace_state(_user())
