import pytest

from hermes_cli import main as hermes_main
from hermes_cli import product_main


def test_hermes_core_setup_dispatches(monkeypatch):
    called = {}

    monkeypatch.setattr(
        "hermes_cli.product_setup.run_product_setup_wizard",
        lambda args: called.setdefault("section", args.section),
    )

    product_main.main(["setup", "tools"])

    assert called["section"] == "tools"


def test_hermes_core_install_dispatches(monkeypatch):
    called = {}

    monkeypatch.setattr(
        "hermes_cli.product_install.run_product_install",
        lambda args: called.setdefault("skip_setup", args.skip_setup),
    )

    product_main.main(["install", "--skip-setup"])

    assert called["skip_setup"] is True


def test_hermes_core_main_exits_cleanly_on_runtime_error(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.product_install.run_product_install",
        lambda args: (_ for _ in ()).throw(RuntimeError("Docker is not available")),
    )

    with pytest.raises(SystemExit, match="Docker is not available"):
        product_main.main(["install", "--skip-setup"])


def test_hermes_cli_rejects_product_subcommand(monkeypatch):
    monkeypatch.setattr("sys.argv", ["hermes", "product"])

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.main()

    assert excinfo.value.code == 2
