from hermes_cli.product_config import (
    ensure_product_home,
    get_product_storage_root,
    get_product_users_root,
    initialize_product_config_file,
    load_product_config,
)


def test_load_product_config_defaults_to_tsidp_tailnet_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = load_product_config()

    assert config["auth"]["provider"] == "tsidp"
    assert config["auth"]["mode"] == "oidc"
    assert config["network"]["tailscale"]["enabled"] is True
    assert config["network"]["tailscale"]["api_tailnet_name"] == ""
    assert config["network"]["tailscale"]["idp_hostname"] == "idp"
    assert config["services"]["tsidp"]["container_name"] == "hermes-tsidp"
    assert config["services"]["tsidp"]["api_token_ref"] == "HERMES_PRODUCT_TAILSCALE_API_TOKEN"


def test_initialize_product_config_file_creates_tailnet_bootstrap_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = initialize_product_config_file()

    assert config["bootstrap"]["first_admin_tailscale_login"] == ""
    assert get_product_storage_root().exists()
    assert get_product_users_root().exists()


def test_ensure_product_home_creates_services_and_bootstrap_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    ensure_product_home()

    product_root = get_product_storage_root()
    assert (product_root / "services").is_dir()
    assert (product_root / "bootstrap").is_dir()
