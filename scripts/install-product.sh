#!/bin/bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'
BOLD='\033[1m'

REPO_OWNER="ErniConcepts"
REPO_NAME="hermes-agent-core"
DEFAULT_BRANCH="main"
PYTHON_VERSION="3.11"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
INSTALL_DIR="${HERMES_INSTALL_DIR:-$HERMES_HOME/hermes-core}"
VENV_DIR="$INSTALL_DIR/.venv"
USER_BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
BRANCH="$DEFAULT_BRANCH"
RUN_SETUP=true
SOURCE_DIR_OVERRIDE="${HERMES_CORE_SOURCE_DIR:-}"
SOURCE_URL_OVERRIDE="${HERMES_CORE_SOURCE_URL:-}"
DOCKER_GROUP_RELOGIN_EXIT_CODE=42

log_info() {
    echo -e "${CYAN}→${NC} $1"
}

log_success() {
    echo -e "${GREEN}✓${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

print_banner() {
    echo ""
    echo -e "${MAGENTA}${BOLD}⚕ Hermes Core Installer${NC}"
    echo ""
}

usage() {
    cat <<EOF
Hermes Core installer

Usage:
  install-product.sh [OPTIONS]

Options:
  --branch NAME    Git branch to install (default: ${DEFAULT_BRANCH})
  --dir PATH       Installation directory (default: ${INSTALL_DIR})
  --skip-setup     Skip the interactive product setup wizard
  -h, --help       Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)
            BRANCH="$2"
            shift 2
            ;;
        --dir)
            INSTALL_DIR="$2"
            VENV_DIR="$INSTALL_DIR/.venv"
            shift 2
            ;;
        --skip-setup)
            RUN_SETUP=false
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

require_cmd() {
    local name="$1"
    if ! command -v "$name" >/dev/null 2>&1; then
        log_error "Required command missing: $name"
        exit 1
    fi
}

ensure_linux() {
    if [[ "$(uname -s)" != "Linux" ]]; then
        log_error "The Hermes Core installer currently supports Linux only"
        exit 1
    fi
    if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        log_error "Run this installer as your normal user, not with sudo."
        log_info "The installer will prompt for sudo when it needs host-level changes."
        exit 1
    fi
}

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        UV_CMD="uv"
        return
    fi
    if [[ -x "$HOME/.local/bin/uv" ]]; then
        UV_CMD="$HOME/.local/bin/uv"
        return
    fi
    log_info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    if [[ -x "$HOME/.local/bin/uv" ]]; then
        UV_CMD="$HOME/.local/bin/uv"
        return
    fi
    if command -v uv >/dev/null 2>&1; then
        UV_CMD="uv"
        return
    fi
    log_error "uv installation failed"
    exit 1
}

ensure_python() {
    if "$UV_CMD" python find "$PYTHON_VERSION" >/dev/null 2>&1; then
        PYTHON_BIN="$("$UV_CMD" python find "$PYTHON_VERSION")"
        return
    fi
    log_info "Installing Python ${PYTHON_VERSION} via uv..."
    "$UV_CMD" python install "$PYTHON_VERSION"
    PYTHON_BIN="$("$UV_CMD" python find "$PYTHON_VERSION")"
}

download_source() {
    if [[ -n "$SOURCE_DIR_OVERRIDE" ]]; then
        log_info "Installing from local source directory: $SOURCE_DIR_OVERRIDE"
        rm -rf "$INSTALL_DIR"
        mkdir -p "$INSTALL_DIR"
        tar -C "$SOURCE_DIR_OVERRIDE" \
            --exclude=.git \
            --exclude=.venv \
            --exclude=.pytest_cache \
            --exclude=__pycache__ \
            -cf - . | tar -C "$INSTALL_DIR" -xf -
        return
    fi

    local tmp_dir
    tmp_dir="$(mktemp -d)"
    local tarball="$tmp_dir/source.tar.gz"
    local url="${SOURCE_URL_OVERRIDE:-https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/heads/${BRANCH}}"

    if [[ -n "$SOURCE_URL_OVERRIDE" ]]; then
        log_info "Downloading installer source from override URL..."
    else
        log_info "Downloading ${REPO_OWNER}/${REPO_NAME}@${BRANCH}..."
    fi
    curl -fsSL "$url" -o "$tarball"

    rm -rf "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    tar -xzf "$tarball" -C "$tmp_dir"
    local extracted
    extracted="$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    cp -R "$extracted"/. "$INSTALL_DIR"/
    rm -rf "$tmp_dir"
}

install_package() {
    log_info "Creating virtual environment..."
    "$UV_CMD" venv --python "$PYTHON_BIN" "$VENV_DIR"
    "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
    "$VENV_DIR/bin/python" -m pip install "$INSTALL_DIR"
}

install_launcher() {
    mkdir -p "$USER_BIN_DIR"
    cat > "$USER_BIN_DIR/hermes-core" <<EOF
#!/bin/bash
export HERMES_CORE_INSTALL_DIR="$INSTALL_DIR"
exec "$VENV_DIR/bin/hermes-core" "\$@"
EOF
    chmod +x "$USER_BIN_DIR/hermes-core"
}

docker_access_ready() {
    if ! getent group docker >/dev/null 2>&1; then
        return 1
    fi
    if id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
        return 0
    fi
    if getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$USER"; then
        return 0
    fi
    return 1
}

run_product_install() {
    local -a install_cmd=("$VENV_DIR/bin/hermes-core" "install")
    local status=0
    if [[ "$RUN_SETUP" == false ]]; then
        install_cmd+=("--skip-setup")
    fi
    export HERMES_CORE_INSTALL_DIR="$INSTALL_DIR"

    log_info "Running Hermes Core install..."
    log_info "This step may prompt for your sudo password."

    if docker_access_ready && command -v sg >/dev/null 2>&1 && ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
        local quoted_cmd
        printf -v quoted_cmd '%q ' "${install_cmd[@]}"
        quoted_cmd="${quoted_cmd% }"
        log_info "Starting Hermes Core install in a docker group shell..."
        sg docker -c "$quoted_cmd"
    else
        set +e
        "${install_cmd[@]}"
        status=$?
        set -e
        if [[ $status -eq 0 ]]; then
            return
        fi
        if command -v sg >/dev/null 2>&1 && [[ $status -eq $DOCKER_GROUP_RELOGIN_EXIT_CODE ]]; then
            local quoted_cmd
            printf -v quoted_cmd '%q ' "${install_cmd[@]}"
            quoted_cmd="${quoted_cmd% }"
            log_info "Retrying Hermes Core install in a docker group shell..."
            sg docker -c "$quoted_cmd"
            return
        fi
        return "$status"
    fi
}

print_post_install() {
    log_success "Hermes Core installed"
    if ! echo ":$PATH:" | grep -q ":$USER_BIN_DIR:"; then
        log_warn "$USER_BIN_DIR is not on PATH in this shell"
        echo "Add this to your shell profile:"
        echo "  export PATH=\"$USER_BIN_DIR:\$PATH\""
    fi
    echo ""
    echo "Fresh install (run as your normal user; the installer prompts for sudo when needed):"
    echo "  curl -fsSL https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${BRANCH}/scripts/install-product.sh | bash"
    echo ""
    echo "Cleanup:"
    echo "  hermes-core uninstall --yes"
}

main() {
    print_banner
    ensure_linux
    require_cmd curl
    require_cmd sudo
    ensure_uv
    ensure_python
    download_source
    install_package
    install_launcher
    run_product_install
    print_post_install
}

main "$@"
