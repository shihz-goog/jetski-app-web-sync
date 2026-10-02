#!/usr/bin/env python3
"""
Jetski App (SSH) <-> Jetski Web (Hub) Daemon for Scenario 1 & Scenario 2:
1) Scenario 1 (Project & Workspace Sync):
   - Auto-discovers workspaces from active SSH IDE processes and trajectory Field 7 (workspace_uris).
   - Auto-creates missing Web Project JSON files in ~/.gemini/config/projects/<uuid>.json
     and calls CreateProject RPC on jetski-hub-server so new SSH repos appear in Web Sidebar.
   - Exports ~/.gemini/jetski/workspaces/<project_name>.code-workspace for 1-click opening in App.
   - Performs longest-prefix workspace matching and binary Protobuf Field 18 (project_id)
     stitching (rescuing conversations stuck in 'outside-of-project').
2) Scenario 2 (Same-Project Cross-Client Conversation Sync):
   - Dynamically discovers live ports and CSRF tokens for jetski-hub-server and SSH language_servers.
   - Invokes LanguageServerService/LoadTrajectory for newly created or updated conversations.
"""

import glob
import json
import os
import re
import sqlite3
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

HOME = os.path.expanduser("~")
BASE_DIR = os.path.join(HOME, ".gemini", "jetski")
CONV_DIR = os.path.join(BASE_DIR, "conversations")
SUM_DB = os.path.join(BASE_DIR, "conversation_summaries.db")
PROJECTS_DIR = os.path.join(HOME, ".gemini", "config", "projects")
WORKSPACES_EXPORT_DIR = os.path.join(BASE_DIR, "workspaces")
STANDALONE_PROJECT_ID = "outside-of-project"


def parse_proto_fields(blob: bytes) -> dict:
    """Minimal zero-dependency Protobuf wire parser."""
    fields = {}
    if not isinstance(blob, bytes):
        return fields
    i = 0
    n = len(blob)
    try:
        while i < n:
            tag = 0
            shift = 0
            while True:
                b = blob[i]
                i += 1
                tag |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            field_num = tag >> 3
            wire_type = tag & 0x7
            if wire_type == 0:
                val = 0
                shift = 0
                while True:
                    b = blob[i]
                    i += 1
                    val |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                fields.setdefault(field_num, []).append(val)
            elif wire_type == 2:
                length = 0
                shift = 0
                while True:
                    b = blob[i]
                    i += 1
                    length |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                sub = blob[i : i + length]
                i += length
                fields.setdefault(field_num, []).append(sub)
            elif wire_type == 1:
                i += 8
            elif wire_type == 5:
                i += 4
            else:
                break
    except Exception:
        pass
    return fields


def extract_workspace_uri(fields: dict) -> str:
    """Extracts the primary file:// workspace URI from CortexTrajectoryMetadata Field 7."""
    for ws_blob in fields.get(7, []):
        if not isinstance(ws_blob, bytes):
            continue
        ws_sub = parse_proto_fields(ws_blob)
        if 1 in ws_sub and ws_sub[1] and isinstance(ws_sub[1][0], bytes):
            uri = ws_sub[1][0].decode("utf-8", "ignore").strip()
            if uri.startswith("file://"):
                return uri.rstrip("/")
        raw = ws_blob.decode("utf-8", "ignore").strip()
        if raw.startswith("file://"):
            return raw.rstrip("/")
    return ""


def extract_project_id(fields: dict) -> str:
    """Extracts Field 18 (project_id) from CortexTrajectoryMetadata."""
    vals = fields.get(18, [])
    if not vals:
        return ""
    last = vals[-1]
    if isinstance(last, bytes):
        return last.decode("utf-8", "ignore").strip()
    return ""


def discover_servers():
    """Dynamically discovers jetski-hub-server (Web) and language_server_linux_x64 (SSH App) ports & CSRF tokens."""
    hub_info = None
    ide_servers = []
    try:
        ss_out = subprocess.check_output(["ss", "-tlnp"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return None, []

    pid_ports = {}
    for line in ss_out.splitlines():
        m_port = re.search(r"(?:127\.0\.0\.1|\*):(\d+)", line)
        m_pids = re.findall(r"pid=(\d+)", line)
        if m_port and m_pids:
            port = int(m_port.group(1))
            if port in (8080, 5387, 37997, 37998, 37999):
                continue
            for pid in m_pids:
                pid_ports.setdefault(pid, []).append(port)

    env_ls_csrf = {}
    for env_path in glob.glob("/proc/[0-9]*/environ"):
        try:
            if os.stat(env_path).st_uid != os.getuid():
                continue
            with open(env_path, "rb") as f:
                raw = f.read().split(b"\x00")
            ls_addr = None
            csrf = None
            for item in raw:
                if item.startswith(b"ANTIGRAVITY_LS_ADDRESS="):
                    ls_addr = item.split(b"=", 1)[1].decode("utf-8", "ignore")
                elif item.startswith(b"ANTIGRAVITY_CSRF_TOKEN="):
                    csrf = item.split(b"=", 1)[1].decode("utf-8", "ignore")
            if csrf and ls_addr and ":" in ls_addr:
                p = int(ls_addr.rsplit(":", 1)[1])
                env_ls_csrf[p] = csrf
        except Exception:
            continue

    fallback_hub_csrf = None
    try:
        with urllib.request.urlopen("http://127.0.0.1:5387/", timeout=1.5) as resp:
            html = resp.read().decode("utf-8", "ignore")
            m_cfg = re.search(r'"csrfToken"\s*:\s*"([0-9a-fA-F-]+)"', html)
            if m_cfg:
                fallback_hub_csrf = m_cfg.group(1)
    except Exception:
        pass
    if not fallback_hub_csrf and os.path.exists("/tmp/jetski_hub_server.ERR"):
        try:
            with open("/tmp/jetski_hub_server.ERR", "r", errors="ignore") as f:
                matches = re.findall(r"CSRFToken:\s*([0-9a-fA-F-]+)", f.read())
                if matches:
                    fallback_hub_csrf = matches[-1]
        except Exception:
            pass

    for pid, ports in pid_ports.items():
        try:
            if os.stat(f"/proc/{pid}").st_uid != os.getuid():
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except Exception:
            continue

        if "jetski-hub-server" in cmd:
            csrf = None
            for p in ports:
                if p in env_ls_csrf:
                    csrf = env_ls_csrf[p]
                    break
            if not csrf:
                m_csrf = re.search(r"--csrf_token[= ]([0-9a-fA-F-]+)", cmd)
                csrf = m_csrf.group(1) if m_csrf else fallback_hub_csrf
            if csrf and ports:
                hub_info = {"pid": pid, "ports": sorted(set(ports)), "csrf": csrf}
        elif "language_server" in cmd and "--subclient_type ide" in cmd and "jetski_lsp_only" not in cmd:
            m_csrf = re.search(r"--csrf_token\s+([0-9a-fA-F-]+)", cmd)
            m_ws = re.search(r"--workspace_id\s+(\S+)", cmd)
            if m_csrf and ports:
                ide_servers.append(
                    {
                        "pid": pid,
                        "ports": sorted(set(ports)),
                        "csrf": m_csrf.group(1),
                        "workspace_id": m_ws.group(1) if m_ws else "",
                    }
                )
    return hub_info, ide_servers


def call_rpc(ports, csrf, method, payload, timeout=8):
    """Calls a LanguageServerService RPC over HTTP or HTTPS."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "x-codeium-csrf-token": csrf,
    }
    for port in ports:
        for scheme in ("http", "https"):
            url = f"{scheme}://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{method}"
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                if scheme == "https":
                    resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
                else:
                    resp = urllib.request.urlopen(req, timeout=timeout)
                if resp.status == 200:
                    return True
            except Exception:
                continue
    return False


def load_workspace_project_map():
    """
    Loads all active projects from ~/.gemini/config/projects/*.json.
    Returns:
      - ws_to_proj: dict mapping folderUri (normalized without trailing slash) -> 36-char project_id
      - existing_names: set of lowercased project names
    """
    ws_to_proj = {}
    existing_names = set()
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    os.makedirs(WORKSPACES_EXPORT_DIR, exist_ok=True)

    for pfile in sorted(glob.glob(os.path.join(PROJECTS_DIR, "*.json"))):
        try:
            with open(pfile, "r", encoding="utf-8") as f:
                pdata = json.load(f)
            pid = pdata.get("id", "")
            pname = pdata.get("name", "").strip()
            if pname:
                existing_names.add(pname.lower())
            if pdata.get("archived") is True or len(pid) != 36:
                continue

            uris = list(pdata.get("workspaceUris", []))
            for res in pdata.get("projectResources", {}).get("resources", []):
                gf = res.get("gitFolder", {}) or res.get("folder", {})
                if gf.get("folderUri"):
                    uris.append(gf["folderUri"])
                elif res.get("folderUri"):
                    uris.append(res["folderUri"])

            clean_paths = []
            for u in uris:
                norm_u = u.rstrip("/")
                ws_to_proj[norm_u] = pid
                if norm_u.startswith("file://"):
                    clean_paths.append(urllib.parse.unquote(norm_u[len("file://") :]))

            # Scenario 1: Export .code-workspace file for easy 1-click opening in Jetski App (SSH)
            if pname and clean_paths and not pname.startswith("/"):
                safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", pname)
                ws_file = os.path.join(WORKSPACES_EXPORT_DIR, f"{safe_name}.code-workspace")
                if not os.path.exists(ws_file):
                    ws_doc = {"folders": [{"path": p} for p in clean_paths], "settings": {}}
                    with open(ws_file, "w", encoding="utf-8") as wf:
                        json.dump(ws_doc, wf, indent=2)
        except Exception:
            continue
    return ws_to_proj, existing_names


def resolve_best_project_id(ws_uri: str, ws_to_proj: dict, existing_names: set, hub_info) -> str:
    """
    Scenario 1: Resolves or auto-creates a Project for a given file:// workspace URI.
    Uses exact match first; if missing and ws_uri is a valid local git/project directory,
    auto-registers a new Project in ~/.gemini/config/projects/<uuid>.json.
    """
    if not ws_uri:
        return ""
    norm_uri = ws_uri.rstrip("/")
    if norm_uri in ws_to_proj:
        return ws_to_proj[norm_uri]

    # Check if local directory exists and is not just $HOME
    if norm_uri.startswith("file://"):
        local_path = urllib.parse.unquote(norm_uri[len("file://") :])
        if os.path.isdir(local_path) and os.path.abspath(local_path) != os.path.abspath(HOME):
            base_name = os.path.basename(local_path) or "workspace"
            candidate_name = base_name
            suffix = 2
            while candidate_name.lower() in existing_names:
                candidate_name = f"{base_name} {suffix}"
                suffix += 1

            new_pid = str(uuid.uuid4())
            is_git = os.path.exists(os.path.join(local_path, ".git"))
            resource_entry = (
                {"gitFolder": {"folderUri": norm_uri, "allowWrite": True}}
                if is_git
                else {"folderUri": norm_uri}
            )
            project_obj = {
                "id": new_pid,
                "name": candidate_name,
                "projectResources": {"resources": [resource_entry]},
                "settings": {},
                "isWorkspaceOnly": False,
            }
            created_via_rpc = False
            if hub_info:
                created_via_rpc = call_rpc(
                    hub_info["ports"],
                    hub_info["csrf"],
                    "CreateProject",
                    {"project": project_obj},
                )
            proj_path = os.path.join(PROJECTS_DIR, f"{new_pid}.json")
            if not created_via_rpc and not os.path.exists(proj_path):
                tmp_path = proj_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(project_obj, f, indent=2)
                os.replace(tmp_path, proj_path)

            ws_to_proj[norm_uri] = new_pid
            existing_names.add(candidate_name.lower())
            return new_pid

    # Fallback: Longest-prefix directory match
    best_prefix = ""
    best_pid = ""
    for registered_uri, pid in ws_to_proj.items():
        if norm_uri.startswith(registered_uri + "/") and len(registered_uri) > len(best_prefix):
            best_prefix = registered_uri
            best_pid = pid
    return best_pid


def get_hub_summary_state():
    state = {}
    if not os.path.exists(SUM_DB):
        return state
    try:
        conn = sqlite3.connect(f"file:{SUM_DB}?mode=ro", uri=True, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT conversation_id, step_count, project_id FROM conversation_summaries")
        for cid, sc, pid in cur.fetchall():
            state[cid] = (sc, pid or "")
        conn.close()
    except Exception:
        pass
    return state


def sync_once(ide_pushed_state: dict):
    hub_info, ide_servers = discover_servers()
    ws_to_proj, existing_names = load_workspace_project_map()
    hub_state = get_hub_summary_state()

    now = time.time()
    for dbpath in glob.glob(os.path.join(CONV_DIR, "*.db")):
        try:
            if now - os.path.getmtime(dbpath) > 7 * 86400:
                continue
        except Exception:
            continue

        cid = os.path.basename(dbpath)[:-3]
        try:
            conn = sqlite3.connect(dbpath, timeout=5)
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM steps")
            step_count = cur.fetchone()[0]
            if step_count == 0:
                conn.close()
                continue

            cur.execute('SELECT data FROM trajectory_metadata_blob WHERE id="main"')
            row = cur.fetchone()
            if not row or not row[0]:
                conn.close()
                continue
            blob = row[0]
            fields = parse_proto_fields(blob)

            # Skip subagent internal trajectories (Field 5 or Field 8)
            if (5 in fields and fields[5]) or (8 in fields and fields[8]):
                conn.close()
                continue

            ws_uri = extract_workspace_uri(fields)
            current_proj_id = extract_project_id(fields)
            target_proj_id = resolve_best_project_id(ws_uri, ws_to_proj, existing_names, hub_info)

            patched = False
            if (
                target_proj_id
                and len(target_proj_id) == 36
                and current_proj_id in ("", STANDALONE_PROJECT_ID)
            ):
                proj_tag = b"\x92\x01\x24" + target_proj_id.encode("utf-8")
                cur.execute('UPDATE trajectory_metadata_blob SET data=? WHERE id="main"', (blob + proj_tag,))
                conn.commit()
                patched = True
            conn.close()

            if hub_info:
                hub_entry = hub_state.get(cid)
                needs_hub_load = (
                    patched
                    or hub_entry is None
                    or (target_proj_id and hub_entry[1] != target_proj_id)
                    or (step_count > hub_entry[0])
                )
                if needs_hub_load:
                    call_rpc(hub_info["ports"], hub_info["csrf"], "LoadTrajectory", {"cascadeId": cid})

            norm_ws = re.sub(r"[^a-zA-Z0-9]", "_", ws_uri.replace("file://", "file")) if ws_uri else ""
            for ide in ide_servers:
                if norm_ws and ide["workspace_id"] and norm_ws != ide["workspace_id"]:
                    continue
                key = (ide["pid"], cid)
                if ide_pushed_state.get(key) != step_count:
                    if call_rpc(ide["ports"], ide["csrf"], "LoadTrajectory", {"cascadeId": cid}):
                        ide_pushed_state[key] = step_count
        except Exception:
            continue


def main():
    ide_pushed_state = {}
    while True:
        try:
            sync_once(ide_pushed_state)
        except Exception:
            pass
        time.sleep(8)


if __name__ == "__main__":
    main()
