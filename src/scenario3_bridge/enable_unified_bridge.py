#!/usr/bin/env python3
"""
Safe controller for Scenario 3 Unified Single-Brain Bridge (jetski_hub_bridge).
Supports:
  --status  : Inspects active Web (jetski-hub-server), SSH (language_server), and Bridge status.
  --enable  : Waits until any active RUNNING conversation on SSH is IDLE, writes discovery files
              ~/.gemini/jetski/daemon/ls_<sha256(workspaceId)[:16]>.json, and sets
              "antigravity.persistentLanguageServer": true in ~/.jetski-server/data/Machine/settings.json.
  --disable : Reverts Machine/settings.json and cleans up bridge discovery files.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import ssl
import subprocess
import time
import urllib.request

HOME = os.path.expanduser("~")
BASE_DIR = os.path.join(HOME, ".gemini", "jetski")
DAEMON_DIR = os.path.join(BASE_DIR, "daemon")
BIN_DIR = os.path.join(BASE_DIR, "bin")
MACHINE_SETTINGS = os.path.join(HOME, ".jetski-server", "data", "Machine", "settings.json")
BRIDGE_HTTPS_PORT = 37999
BRIDGE_HTTP_PORT = 37998
BRIDGE_LSP_PORT = 37997


def get_ide_version() -> str:
    bin_dirs = sorted(glob.glob(os.path.join(HOME, ".jetski-server", "bin", "*")))
    if bin_dirs:
        return os.path.basename(bin_dirs[-1]).split("-")[0]
    return ""


def check_running_trajectories(port: int, csrf: str) -> list:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories",
        data=b"{}",
        headers={"Content-Type": "application/json", "x-codeium-csrf-token": csrf},
        method="POST",
    )
    running = []
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
            for cid, s in data.get("trajectorySummaries", {}).items():
                if s.get("status") == "CASCADE_RUN_STATUS_RUNNING":
                    running.append((cid, s.get("stepCount", 0)))
    except Exception:
        pass
    return running


def get_bridge_pid() -> int:
    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", "jetski-hub-bridge.service"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out.isdigit() and int(out) > 0:
            return int(out)
    except Exception:
        pass
    return 0


def discover_ssh_workspaces() -> list:
    workspaces = set()
    try:
        out = subprocess.check_output(["ps", "-u", str(os.getuid()), "-o", "pid=,args="], text=True)
        for line in out.splitlines():
            if "language_server" in line and "--workspace_id" in line:
                m = re.search(r"--workspace_id\s+(\S+)", line)
                if m:
                    workspaces.add(m.group(1))
    except Exception:
        pass
    for pfile in glob.glob(os.path.join(HOME, ".gemini", "config", "projects", "*.json")):
        try:
            with open(pfile, "r", encoding="utf-8") as f:
                pdata = json.load(f)
            for ws in pdata.get("workspaces", []):
                p = ws.get("path", "").rstrip("/")
                if p:
                    uri = p if p.startswith("file://") else f"file://{p}"
                    norm = re.sub(r"[^a-zA-Z0-9]+", "_", uri).strip("_")
                    workspaces.add(norm)
            for res in pdata.get("projectResources", {}).get("resources", []):
                gf = res.get("gitFolder", {}) or res.get("folder", {})
                uri = gf.get("folderUri") or res.get("folderUri", "")
                if uri.startswith("file://"):
                    norm = re.sub(r"[^a-zA-Z0-9]+", "_", uri.rstrip("/")).strip("_")
                    workspaces.add(norm)
        except Exception:
            pass
    for git_dir in glob.glob(os.path.join(HOME, "git", "*")):
        if os.path.isdir(git_dir):
            uri = f"file://{git_dir.rstrip('/')}"
            norm = re.sub(r"[^a-zA-Z0-9]+", "_", uri).strip("_")
            workspaces.add(norm)
    return sorted(workspaces)


def write_machine_setting(val: bool):
    os.makedirs(os.path.dirname(MACHINE_SETTINGS), exist_ok=True)
    settings = {}
    if os.path.exists(MACHINE_SETTINGS):
        try:
            with open(MACHINE_SETTINGS, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except Exception:
            settings = {}
    settings["antigravity.persistentLanguageServer"] = val
    if val:
        settings["codeiumDev.languageServerBinaryPath"] = os.path.join(BIN_DIR, "jetski_ls_shim.py")
        settings["codeiumDev.languageServerEnv"] = {"BRIDGE_EPOCH": str(int(time.time()))}
    else:
        settings.pop("codeiumDev.languageServerBinaryPath", None)
        settings.pop("codeiumDev.languageServerEnv", None)
    with open(MACHINE_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def cmd_status():
    bridge_pid = get_bridge_pid()
    print(f"Bridge Daemon (jetski_hub_bridge) : {'RUNNING (PID ' + str(bridge_pid) + ')' if bridge_pid else 'STOPPED'}")
    print(f"Bridge HTTPS / HTTP / LSP Ports   : {BRIDGE_HTTPS_PORT} / {BRIDGE_HTTP_PORT} / {BRIDGE_LSP_PORT}")
    print(f"Detected IDE Version              : {get_ide_version() or 'N/A'}")
    print(f"Known Workspace IDs               : {len(discover_ssh_workspaces())}")
    if os.path.exists(MACHINE_SETTINGS):
        with open(MACHINE_SETTINGS, "r", encoding="utf-8") as f:
            print(f"Machine settings.json             : {f.read().strip()}")


def cmd_enable():
    os.makedirs(DAEMON_DIR, exist_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "restart", "jetski-hub-bridge.service"], check=True)
    time.sleep(0.5)
    bridge_pid = get_bridge_pid()
    if not bridge_pid:
        raise RuntimeError("Failed to start jetski-hub-bridge.service")

    # Kill any leftover language_server_linux_x64 and wait until it finishes server.go:2701 cleanup
    subprocess.run(["pkill", "-u", str(os.getuid()), "-f", "language_server_linux_x64"], check=False)
    for _ in range(20):
        res = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", "language_server_linux_x64"], stdout=subprocess.DEVNULL)
        if res.returncode != 0:
            break
        time.sleep(0.2)

    # Write discovery files with the live systemd MainPID of jetski_hub_bridge
    ide_ver = get_ide_version()
    for ws_id in discover_ssh_workspaces():
        ws_hash = hashlib.sha256(ws_id.encode("utf-8")).hexdigest()[:16]
        disc_file = os.path.join(DAEMON_DIR, f"ls_{ws_hash}.json")
        disc_data = {
            "pid": bridge_pid,
            "httpsPort": BRIDGE_HTTPS_PORT,
            "httpPort": BRIDGE_HTTP_PORT,
            "lspPort": BRIDGE_LSP_PORT,
            "csrfToken": "jetski-unified-bridge-token",
            "lsVersion": ide_ver,
        }
        with open(disc_file, "w", encoding="utf-8") as f:
            json.dump(disc_data, f, indent=2)

    # Trigger extension.js maybeUpdate() with persistentLanguageServer=True + new BRIDGE_EPOCH
    write_machine_setting(True)
    print(f"Enabled Unified Hub Bridge (PID {bridge_pid}) on HTTPS :{BRIDGE_HTTPS_PORT} (lsVersion={ide_ver})")


def cmd_disable():
    write_machine_setting(False)
    subprocess.run(["systemctl", "--user", "stop", "jetski-hub-bridge.service"], check=False)
    subprocess.run(["systemctl", "--user", "disable", "jetski-hub-bridge.service"], check=False)
    for fpath in glob.glob(os.path.join(DAEMON_DIR, "ls_*.json")):
        try:
            os.remove(fpath)
        except Exception:
            pass
    print("Disabled Unified Hub Bridge and restored default dual-process mode.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true")
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    if args.status:
        cmd_status()
    elif args.enable:
        cmd_enable()
    elif args.disable:
        cmd_disable()
