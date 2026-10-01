#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Jetski App (SSH) <-> Web (Hub) Conversation Sync Uninstaller
# Cleanly stops services, removes bridge discovery files, and restores default
# dual-process SSH behavior.
# ==============================================================================

JETSKI_BIN_DIR="${HOME}/.gemini/jetski/bin"
DAEMON_DIR="${HOME}/.gemini/jetski/daemon"

echo "[1/3] Stopping Scenario 1 & 2 Sync Daemon..."
systemctl --user disable --now jetski-conv-sync.service 2>/dev/null || true

echo "[2/3] Stopping Scenario 3 Single-Brain Bridge and cleaning discovery files..."
pkill -f "${JETSKI_BIN_DIR}/jetski_hub_bridge" 2>/dev/null || true
rm -f "${DAEMON_DIR}"/ls_*.json 2>/dev/null || true

echo "[3/3] Restoring VS Code Remote settings..."
python3 - <<'PY'
import json
from pathlib import Path

for settings_path in [
    Path.home() / ".jetski-server/data/Machine/settings.json",
    Path.home() / ".jetski-server/data/User/settings.json",
]:
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
            if "antigravity.persistentLanguageServer" in data:
                data["antigravity.persistentLanguageServer"] = False
                settings_path.write_text(json.dumps(data, indent=2) + "\n")
                print(f"  -> Disabled antigravity.persistentLanguageServer in {settings_path}")
        except Exception as e:
            print(f"  -> Warning: failed to update {settings_path}: {e}")
PY

echo "Uninstall complete! Reload your Jetski App (SSH) window to spawn a standard standalone Language Server."
