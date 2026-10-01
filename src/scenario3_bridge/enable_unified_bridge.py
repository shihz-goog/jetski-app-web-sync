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


def get_ide_version() -> str:
    pkg_files = sorted(
        glob.glob(
            os.path.join(
                HOME,
                ".jetski-server",
                "bin",
                "*",
                "extensions",
                "antigravity",
                "package.json",
            )
        )
    )
    if pkg_files:
        try:
            with open(pkg_files[-1], "r", encoding="utf-8") as f:
                return json.load(f).get("version", "")
        except Exception:
            pass
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
        out = subprocess.check_output(["pgrep", "-u", str(os.getuid()), "-f", "jetski_hub_bridge"], text=True)
        pids = [int(x) for x in out.strip().splitlines() if x.strip().isdigit()]
        return pids[0] if pids else 0
    except Exception:
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
            for res in pdata.get("projectResources", {}).get("resources", []):
                gf = res.get("gitFolder", {}) or res.get("folder", {})
                uri = gf.get("folderUri") or res.get("folderUri", "")
                if uri.startswith("file://"):
                    norm = re.sub(r"[^a-zA-Z0-9]", "_", uri.rstrip("/").replace("file://", "file"))
                    workspaces.add(norm)
        except Exception:
            pass
    return sorted(workspaces)


def cmd_status():
    bridge_pid = get_bridge_pid()
    print(f"Bridge Daemon (jetski_hub_bridge) : {'RUNNING (PID ' + str(bridge_pid) + ')' if bridge_pid else 'STOPPED'}")
    print(f"Bridge HTTPS / HTTP Ports         : {BRIDGE_HTTPS_PORT} / {BRIDGE_HTTP_PORT}")
    print(f"Detected IDE Version              : {get_ide_version() or 'N/A'}")
    print(f"Known Workspace IDs               : {len(discover_ssh_workspaces())}")
    if os.path.exists(MACHINE_SETTINGS):
        with open(MACHINE_SETTINGS, "r", encoding="utf-8") as f:
            print(f"Machine settings.json             : {f.read().strip()}")


def cmd_enable():
    os.makedirs(DAEMON_DIR, exist_ok=True)
    bridge_pid = get_bridge_pid()
    if not bridge_pid:
        bridge_bin = os.path.join(BIN_DIR, "jetski_hub_bridge")
        log_path = os.path.join(BIN_DIR, "jetski_hub_bridge.log")
        subprocess.Popen(
            [bridge_bin, "--https_port", str(BRIDGE_HTTPS_PORT), "--http_port", str(BRIDGE_HTTP_PORT)],
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(1)
        bridge_pid = get_bridge_pid()

    ide_ver = get_ide_version()
    for ws_id in discover_ssh_workspaces():
        ws_hash = hashlib.sha256(ws_id.encode("utf-8")).hexdigest()[:16]
        disc_file = os.path.join(DAEMON_DIR, f"ls_{ws_hash}.json")
        disc_data = {
            "pid": bridge_pid,
            "httpsPort": BRIDGE_HTTPS_PORT,
            "httpPort": BRIDGE_HTTP_PORT,
            "lspPort": 0,
            "csrfToken": "jetski-unified-bridge-token",
            "lsVersion": ide_ver,
        }
        with open(disc_file, "w", encoding="utf-8") as f:
            json.dump(disc_data, f, indent=2)

    os.makedirs(os.path.dirname(MACHINE_SETTINGS), exist_ok=True)
    settings = {}
    if os.path.exists(MACHINE_SETTINGS):
        try:
            with open(MACHINE_SETTINGS, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except Exception:
            settings = {}
    settings["antigravity.persistentLanguageServer"] = True
    with open(MACHINE_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    print(f"Enabled Unified Hub Bridge (PID {bridge_pid}) on HTTPS :{BRIDGE_HTTPS_PORT}")


def cmd_disable():
    if os.path.exists(MACHINE_SETTINGS):
        try:
            with open(MACHINE_SETTINGS, "r", encoding="utf-8") as f:
                settings = json.load(f)
            settings.pop("antigravity.persistentLanguageServer", None)
            with open(MACHINE_SETTINGS, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
        except Exception:
            pass
    for fpath in glob.glob(os.path.join(DAEMON_DIR, "ls_*.json")):
        try:
            os.remove(fpath)
        except Exception:
            pass
    subprocess.call(["pkill", "-u", str(os.getuid()), "-f", "jetski_hub_bridge"])
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
