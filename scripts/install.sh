#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Jetski App (SSH) <-> Web (Hub) Conversation Sync Installer
# Supports:
#   --mode daemon : Scenario 1 (Project/Workspace) + Scenario 2 (Conversation List)
#   --mode bridge : Scenario 1 + Scenario 2 + Scenario 3 (Single-Brain Bridge)
# ==============================================================================

MODE="bridge"
AUTO_CREATE_SUBPROJECT="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"
      shift 2
      ;;
    --auto-create-subproject)
      AUTO_CREATE_SUBPROJECT="1"
      shift
      ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: $0 [--mode daemon|bridge] [--auto-create-subproject]"
      exit 1
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JETSKI_BIN_DIR="${HOME}/.gemini/jetski/bin"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

mkdir -p "${JETSKI_BIN_DIR}" "${SYSTEMD_USER_DIR}"

echo "[1/3] Installing Scenario 1 & 2 Sync Daemon..."
cp "${REPO_ROOT}/src/scenario1_2_daemon/jetski_conv_sync_daemon.py" "${JETSKI_BIN_DIR}/jetski_conv_sync_daemon.py"
chmod +x "${JETSKI_BIN_DIR}/jetski_conv_sync_daemon.py"

cp "${REPO_ROOT}/src/scenario1_2_daemon/jetski-conv-sync.service" "${SYSTEMD_USER_DIR}/jetski-conv-sync.service"
if [[ "${AUTO_CREATE_SUBPROJECT}" == "1" ]]; then
  sed -i 's/JETSKI_AUTO_CREATE_SUBPROJECT=0/JETSKI_AUTO_CREATE_SUBPROJECT=1/g' "${SYSTEMD_USER_DIR}/jetski-conv-sync.service"
fi

systemctl --user daemon-reload
systemctl --user enable --now jetski-conv-sync.service
echo "  -> Service jetski-conv-sync.service is active."

if [[ "${MODE}" == "bridge" ]]; then
  echo "[2/3] Building & Installing Scenario 3 Single-Brain HTTP/2 Bridge..."
  cp "${REPO_ROOT}/src/scenario3_bridge/extract_local_cert.py" "${JETSKI_BIN_DIR}/extract_local_cert.py"
  cp "${REPO_ROOT}/src/scenario3_bridge/jetski_hub_bridge.go" "${JETSKI_BIN_DIR}/jetski_hub_bridge.go"
  cp "${REPO_ROOT}/src/scenario3_bridge/jetski_ls_shim.py" "${JETSKI_BIN_DIR}/jetski_ls_shim.py"
  cp "${REPO_ROOT}/src/scenario3_bridge/enable_unified_bridge.py" "${JETSKI_BIN_DIR}/enable_unified_bridge.py"
  cp "${REPO_ROOT}/src/scenario3_bridge/jetski-hub-bridge.service" "${SYSTEMD_USER_DIR}/jetski-hub-bridge.service"
  chmod +x "${JETSKI_BIN_DIR}/extract_local_cert.py" "${JETSKI_BIN_DIR}/jetski_ls_shim.py" "${JETSKI_BIN_DIR}/enable_unified_bridge.py"

  python3 "${JETSKI_BIN_DIR}/extract_local_cert.py"
  go build -o "${JETSKI_BIN_DIR}/jetski_hub_bridge" "${JETSKI_BIN_DIR}/jetski_hub_bridge.go"
  systemctl --user daemon-reload
  systemctl --user enable jetski-hub-bridge.service
  python3 "${JETSKI_BIN_DIR}/enable_unified_bridge.py" --enable
else
  echo "[2/3] Skipping Scenario 3 Single-Brain Bridge (--mode daemon)."
fi

echo "[3/3] Installation complete!"
echo "Check status with: systemctl --user status jetski-conv-sync.service"
