#!/usr/bin/env python3
"""
Lightweight Language Server Shim for Scenario 3 (jetski_hub_bridge).
Configured via "codeiumDev.languageServerBinaryPath" in ~/.jetski-server/data/Machine/settings.json.

When VS Code's Antigravity extension starts or restarts the Language Server in SSH mode:
1. Parses --csrf_token, --extension_server_port, --extension_server_csrf_token, and --workspace_id.
2. Writes ~/.gemini/jetski/daemon/ls_<sha256(workspace_id)[:16]>.json pointing to jetski_hub_bridge
   (:37999 HTTPS / :37998 HTTP / :37997 LSP) with this process's live PID and exact CSRF token.
3. Registers extension_server_port & extension_server_csrf_token with jetski_hub_bridge so
   UnifiedStateSync updates are pushed back to the VS Code Extension Host.
4. Sleeps until SIGTERM so extension.js sees a healthy, long-lived Language Server process.
"""

import glob
import hashlib
import json
import os
import signal
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
DAEMON_DIR = os.path.join(HOME, ".gemini", "jetski", "daemon")
BRIDGE_HTTPS_PORT = 37999
BRIDGE_HTTP_PORT = 37998
BRIDGE_LSP_PORT = 37997


def get_ide_version() -> str:
    bin_dirs = sorted(glob.glob(os.path.join(HOME, ".jetski-server", "bin", "*")))
    if bin_dirs:
        return os.path.basename(bin_dirs[-1]).split("-")[0]
    return ""


def parse_args(argv):
    csrf_token = "jetski-unified-bridge-token"
    ext_port = 0
    ext_csrf = ""
    workspace_id = ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--csrf_token" and i + 1 < len(argv):
            csrf_token = argv[i + 1]
            i += 2
        elif arg == "--extension_server_port" and i + 1 < len(argv):
            try:
                ext_port = int(argv[i + 1])
            except Exception:
                pass
            i += 2
        elif arg == "--extension_server_csrf_token" and i + 1 < len(argv):
            ext_csrf = argv[i + 1]
            i += 2
        elif arg == "--workspace_id" and i + 1 < len(argv):
            workspace_id = argv[i + 1]
            i += 2
        else:
            i += 1
    return csrf_token, ext_port, ext_csrf, workspace_id


def main():
    # Consume stdin metadata proto sent by extension.js without blocking
    try:
        sys.stdin.buffer.read()
    except Exception:
        pass

    csrf_token, ext_port, ext_csrf, workspace_id = parse_args(sys.argv[1:])
    os.makedirs(DAEMON_DIR, exist_ok=True)
    ide_ver = get_ide_version()

    ws_hash = hashlib.sha256((workspace_id or "").encode("utf-8")).hexdigest()[:16]
    disc_file = os.path.join(DAEMON_DIR, f"ls_{ws_hash}.json")
    disc_data = {
        "pid": os.getpid(),
        "httpsPort": BRIDGE_HTTPS_PORT,
        "httpPort": BRIDGE_HTTP_PORT,
        "lspPort": BRIDGE_LSP_PORT,
        "csrfToken": csrf_token,
        "lsVersion": ide_ver,
    }
    with open(disc_file, "w", encoding="utf-8") as f:
        json.dump(disc_data, f, indent=2)

    if ext_port > 0 and ext_csrf:
        payload = json.dumps(
            {
                "extensionServerPort": ext_port,
                "extensionServerCsrfToken": ext_csrf,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{BRIDGE_HTTP_PORT}/exa.language_server_pb.LanguageServerService/ReconnectExtensionServer",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=2).close()
        except Exception:
            pass

    def handle_sigterm(signum, frame):
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
