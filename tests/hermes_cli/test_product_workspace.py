import pytest

from hermes_cli.product_workspace import (
    ProductWorkspaceQuotaError,
    create_workspace_folder,
    delete_workspace_path,
    get_workspace_state,
    humanize_bytes,
    move_workspace_path,
    store_workspace_file,
)
from hermes_cli.product_runtime import _workspace_root


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


def test_workspace_upload_filename_is_reduced_to_final_segment(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    state = store_workspace_file(
        _user(),
        parent_path="",
        filename="../nested/hello.txt",
        content=b"hello",
    )

    assert [entry.name for entry in state.entries] == ["hello.txt"]


def test_workspace_delete_file_returns_parent_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    create_workspace_folder(_user(), parent_path="", folder_name="reports")
    store_workspace_file(_user(), parent_path="reports", filename="budget.txt", content=b"budget")

    state = delete_workspace_path(_user(), path="reports/budget.txt")

    assert state.current_path == "reports"
    assert state.entries == []


def test_workspace_delete_folder_is_recursive(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    create_workspace_folder(_user(), parent_path="", folder_name="reports")
    create_workspace_folder(_user(), parent_path="reports", folder_name="nested")
    store_workspace_file(_user(), parent_path="reports/nested", filename="budget.txt", content=b"budget")

    state = delete_workspace_path(_user(), path="reports")

    assert state.current_path == ""
    assert state.entries == []


def test_workspace_delete_rejects_empty_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="must not be empty"):
        delete_workspace_path(_user(), path="")


def test_workspace_hides_runtime_tmp_directory_from_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    store_workspace_file(
        _user(),
        parent_path="",
        filename="visible.txt",
        content=b"hello",
    )
    workspace_root = _workspace_root({}, "user-1")
    hidden_tmp = workspace_root / ".tmp"
    hidden_tmp.mkdir(parents=True, exist_ok=True)
    (hidden_tmp / "scratch.txt").write_text("temp", encoding="utf-8")

    state = get_workspace_state(_user())

    assert [entry.name for entry in state.entries] == ["visible.txt"]


def test_workspace_can_move_file_into_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    create_workspace_folder(_user(), parent_path="", folder_name="reports")
    store_workspace_file(_user(), parent_path="", filename="budget.txt", content=b"budget")

    state = move_workspace_path(_user(), source_path="budget.txt", destination_parent_path="reports")

    assert state.current_path == "reports"
    assert [entry.name for entry in state.entries] == ["budget.txt"]


def test_workspace_can_move_file_to_parent_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    create_workspace_folder(_user(), parent_path="", folder_name="reports")
    store_workspace_file(_user(), parent_path="reports", filename="budget.txt", content=b"budget")

    state = move_workspace_path(_user(), source_path="reports/budget.txt", destination_parent_path="")

    assert state.current_path == ""
    assert [entry.name for entry in state.entries] == ["reports", "budget.txt"]


def test_workspace_rejects_move_into_existing_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    create_workspace_folder(_user(), parent_path="", folder_name="reports")
    store_workspace_file(_user(), parent_path="", filename="budget.txt", content=b"budget")
    store_workspace_file(_user(), parent_path="reports", filename="budget.txt", content=b"existing")

    with pytest.raises(ValueError, match="already exists"):
        move_workspace_path(_user(), source_path="budget.txt", destination_parent_path="reports")


def test_workspace_rejects_move_folder_into_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    create_workspace_folder(_user(), parent_path="", folder_name="reports")
    create_workspace_folder(_user(), parent_path="reports", folder_name="nested")

    with pytest.raises(ValueError, match="into itself"):
        move_workspace_path(_user(), source_path="reports", destination_parent_path="reports/nested")
