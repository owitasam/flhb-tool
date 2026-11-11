#!/usr/bin/env bash
#
# install.sh - flhb-tool Packaging Installer
#
# Copies the flhb-tool package to ~/.local/share/flhb-tool/ and sets
# proper execution permissions so it can be used as a Lutris custom
# executable / launch wrapper.
#
# Usage:
#   ./install.sh            # install for the current user
#   ./install.sh --prefix /opt/flhb-tool   # custom install prefix
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The flhb_tool package lives alongside this script.
REPO_ROOT="${SCRIPT_DIR}"
PACKAGE_DIR="${REPO_ROOT}/flhb_tool"

DEFAULT_INSTALL_DIR="${HOME}/.local/share/flhb-tool"
INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
BIN_DIR="${HOME}/.local/bin"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { printf '[flhb-install] %s\n' "$*"; }
warn() { printf '[flhb-install] WARNING: %s\n' "$*" >&2; }
err()  { printf '[flhb-install] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $0 [--prefix DIR] [--bindir DIR] [--help]

Options:
  --prefix DIR   Installation directory (default: ${DEFAULT_INSTALL_DIR})
  --bindir DIR   Directory for executable wrappers (default: ${HOME}/.local/bin)
  --help         Show this help message

Installs flhb-tool so it can be used as a Lutris custom executable wrapper.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            shift
            INSTALL_DIR="${1:-}"
            [[ -n "${INSTALL_DIR}" ]] || err "--prefix requires a directory"
            ;;
        --bindir)
            shift
            BIN_DIR="${1:-}"
            [[ -n "${BIN_DIR}" ]] || err "--bindir requires a directory"
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            err "Unknown option: $1"
            ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || err "python3 is required but not found."
[[ -d "${PACKAGE_DIR}" ]] || err "Package directory not found: ${PACKAGE_DIR}"
[[ -f "${PACKAGE_DIR}/bridge.py" ]] || err "bridge.py not found in ${PACKAGE_DIR}"

log "Python version: $(python3 --version 2>&1)"

# ---------------------------------------------------------------------------
# Install package files
# ---------------------------------------------------------------------------
log "Installing flhb-tool to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"

# Copy the package, excluding __pycache__ directories.
if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude="__pycache__" "${PACKAGE_DIR}/" "${INSTALL_DIR}/"
else
    cp -r "${PACKAGE_DIR}/." "${INSTALL_DIR}/"
    find "${INSTALL_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
fi

# Copy supporting files at repo root (example config, README).
for f in config.json.example README.md; do
    if [[ -f "${REPO_ROOT}/${f}" ]]; then
        cp "${REPO_ROOT}/${f}" "${INSTALL_DIR}/"
    fi
done

# Make bridge.py and any .sh files directly executable.
chmod +x "${INSTALL_DIR}/bridge.py"
chmod +x "${INSTALL_DIR}/"*.sh 2>/dev/null || true

# Also make the package modules importable (no-op for direct execution, but
# good hygiene if something calls python3 -m flhb_tool from the install dir).
chmod 644 "${INSTALL_DIR}"/*.py 2>/dev/null || true
chmod +x "${INSTALL_DIR}/bridge.py" "${INSTALL_DIR}/__main__.py" 2>/dev/null || true

log "Package files installed."

# ---------------------------------------------------------------------------
# Create convenience wrapper in bindir
# ---------------------------------------------------------------------------
mkdir -p "${BIN_DIR}"

WRAPPER="${BIN_DIR}/flhb-bridge"
cat > "${WRAPPER}" <<WRAPPER_EOF
#!/usr/bin/env bash
# flhb-bridge - convenience wrapper for Lutris integration.
# Installed by flhb-tool install.sh.
#
# Usage:
#   flhb-bridge /path/to/config.json
#
# Forwards all arguments to the installed bridge.py.
set -euo pipefail

INSTALLED_BRIDGE="${INSTALL_DIR}/bridge.py"

if [[ ! -x "\${INSTALLED_BRIDGE}" ]]; then
    echo "flhb-bridge: bridge.py not found or not executable at \${INSTALLED_BRIDGE}" >&2
    exit 1
fi

exec python3 "\${INSTALLED_BRIDGE}" "\$@"
WRAPPER_EOF

chmod +x "${WRAPPER}"
log "Wrapper installed to ${WRAPPER}"

# ---------------------------------------------------------------------------
# Create a Lutris-friendly per-game launcher wrapper template
# ---------------------------------------------------------------------------
LUTRIS_WRAPPER="${INSTALL_DIR}/flhb-lutris-wrapper.sh"
cat > "${LUTRIS_WRAPPER}" <<'LUTRIS_WRAPPER_EOF'
#!/usr/bin/env bash
# flhb-lutris-wrapper.sh - Lutris launch wrapper for flhb-tool.
#
# HOW TO USE IN LUTRIS:
#   1. Open your game's Configuration in Lutris.
#   2. Set Runner to "Wine" (or keep your existing runner).
#   3. Under "Game options" -> "Executable", set the main game binary.
#   4. Under "Game options" -> "Wine prefix", set your Lutris prefix path.
#   5. Add a "Pre-launch command" or use the "Script" runner:
#        /path/to/flhb-lutris-wrapper.sh /path/to/config.json
#
# This wrapper runs flhb-tool's pre-flight checks (Phase 1), then launches
# the target executable through GameBridge (Phase 2) while monitoring logs
# for known errors (Phase 3).
#
# Environment variables you can set in Lutris:
#   FLHB_CONFIG      Path to your config.json (required by this wrapper).
#   FLHB_LOG_FILE    Path for flhb_debug.log (default: ./flhb_debug.log).
#   FLHB_AUTO_FIX    Set to "1" to attempt automated remediation (default: 0).
#
set -euo pipefail

# Derive the install directory from this script's own location so the
# wrapper is portable regardless of where flhb-tool is installed.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLED_BRIDGE="${SCRIPT_DIR}/bridge.py"

CONFIG="${FLHB_CONFIG:-}"
LOG_FILE="${FLHB_LOG_FILE:-flhb_debug.log}"
AUTO_FIX="${FLHB_AUTO_FIX:-0}"

if [[ -z "${CONFIG}" ]]; then
    echo "flhb-lutris-wrapper: FLHB_CONFIG is not set. Point it to a config.json." >&2
    exit 1
fi

if [[ ! -f "${CONFIG}" ]]; then
    echo "flhb-lutris-wrapper: config not found: ${CONFIG}" >&2
    exit 1
fi

if [[ ! -x "${INSTALLED_BRIDGE}" ]]; then
    echo "flhb-lutris-wrapper: bridge.py not found or not executable at ${INSTALLED_BRIDGE}" >&2
    exit 1
fi

# Phase 1: pre-flight dependency check (non-fatal; warns but continues).
python3 "${SCRIPT_DIR}/__main__.py" --log-file "${LOG_FILE}" >/dev/null 2>&1 || true

# Phase 2+3: launch + real-time log monitoring.
ARGS=("${CONFIG}" --log-file "${LOG_FILE}")
if [[ "${AUTO_FIX}" == "1" ]]; then
    ARGS+=(--auto-fix)
fi

exec python3 "${INSTALLED_BRIDGE}" "${ARGS[@]}"
LUTRIS_WRAPPER_EOF

chmod +x "${LUTRIS_WRAPPER}"
log "Lutris wrapper installed to ${LUTRIS_WRAPPER}"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
cat <<EOF

============================================================================
flhb-tool installation complete
============================================================================
Install dir : ${INSTALL_DIR}
Wrapper     : ${WRAPPER}
Lutris wrap : ${LUTRIS_WRAPPER}

Next steps:
  1. Copy config.json.example to a per-game config.json and edit it
     (wine_prefix_path, target_executable, loader_arguments,
      environment_overrides).
  2. In Lutris, configure your game to use the flhb-lutris-wrapper.sh
     (see LUTRIS_INTEGRATION.md for step-by-step instructions).
  3. Ensure ~/.local/bin is on your PATH if you want to call flhb-bridge
     directly.

EOF
