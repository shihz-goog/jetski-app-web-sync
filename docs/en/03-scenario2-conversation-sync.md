<div align="center">

# Topic 3 (Scenario 2): Cross-Client Conversation List Synchronization Within the Same Project

**[🇨🇳 简体中文](../03-scenario2-conversation-sync.md) | [🌐 English](./03-scenario2-conversation-sync.md) | [🏠 Home (README)](../../README_EN.md)**

</div>

---

## 1. Scenario Definition & Symptoms

Within the same Project / Workspace (e.g., `my-project`):
- You create **Conversation A** in **Jetski App (SSH)**, then open **Jetski Web** under `my-project` — Conversation A is missing from the sidebar;
- Conversely, you create **Conversation B** in **Jetski Web** under `my-project`, then check **Jetski App (SSH)**'s conversation dropdown — Conversation B is invisible.

---

## 2. Source-Level Root Cause: Why Sharing `conversations/*.db` Is Not Enough

Even though both `jetski-hub-server` (Web) and `language_server_linux_x64` (App) write conversation steps to `$HOME/.gemini/jetski/conversations/<conversation_id>.db`, their **Summary Store indexing mechanisms** are completely decoupled:

### 2.1 Why Conversations Created in App Are Invisible in Web
1. **Missing Field 18 (`project_id`)**: As explained in Topic 2, `.db` files created by App only contain `Field 7 (workspace_uris)` and lack `Field 18 (project_id)`;
2. **App never writes to `conversation_summaries.db`**: Web's sidebar reads `$HOME/.gemini/jetski/conversation_summaries.db`;
3. **Web's `reconcileSummariesOnce` runs ONLY once at process startup**:
   In `third_party/jetski/summarystore/`, `jetski-hub-server` scans `conversations/` and reconciles `conversation_summaries.db` only during initial boot. It **has no `fsnotify` file watcher**, so `.db` files created later by external processes are ignored.

### 2.2 Why Conversations Created in Web Are Invisible in App
- App (`--subclient_type=ide`) **never scans** `conversations/*.db` on disk to populate its conversation list;
- Instead, it relies entirely on its bound `language_server_linux_x64` process calling `ExtensionServerService/PushUnifiedStateSyncUpdate` (`topicName: "trajectorySummaries"`) to push summaries into the laptop's `state.vscdb`. Because Web's conversations are created by `jetski-hub-server`, App's `language_server_linux_x64` has no knowledge of them in RAM.

---

## 3. The Key Mechanism: Native `LanguageServerService/LoadTrajectory` RPC

Inspecting `third_party/jetski/cortex/cascade_manager.go` reveals an internal RPC exposed by both backend processes:

```http
POST /exa.language_server_pb.LanguageServerService/LoadTrajectory
Content-Type: application/json
Connect-Protocol-Version: 1
x-codeium-csrf-token: <process_csrf_token>

{"cascadeId": "<conversation_id>"}
```

When `LoadTrajectory({"cascadeId": cid})` is called on a process where `cid` **is not yet loaded in `cascadeMap` RAM**, the process executes:
1. **`trajectoryStore.LoadUnsafe(ctx, cid)`**: Reads the metadata and all historical steps from `$HOME/.gemini/jetski/conversations/<cid>.db`;
2. **`updateStateForLoadedTrajectory` $\rightarrow$ `m.updateSummary`**:
   - **On Web (`jetski-hub-server`)**: Writes the summary (with the stitched `project_id`) into `conversation_summaries.db`, indexes it in FTS5, and streams the update to the browser sidebar via `StreamCascadeSummariesReactiveUpdates`;
   - **On App (`language_server_linux_x64`)**: Converts the trajectory into a `CascadeTrajectorySummary` and pushes it via `ExtensionServerService/PushUnifiedStateSyncUpdate` to the laptop's `state.vscdb`, making it appear immediately in App's dropdown!

---

## 4. Daemon Implementation (`jetski_conv_sync_daemon.py`)

[`src/scenario1_2_daemon/jetski_conv_sync_daemon.py`](../../src/scenario1_2_daemon/jetski_conv_sync_daemon.py) runs a lightweight cycle every 8 seconds:
1. **Zero-Config Port & CSRF Discovery (`discover_servers`)**: Discovers `jetski-hub-server` and all `language_server_linux_x64 --subclient_type ide` ports and CSRF tokens via `ss -tlnp`, `/proc/<pid>/cmdline`, and `:5387` HTML config;
2. **Subagent Filtering**: Skips internal subagent trajectories (Protobuf Field 5 / Field 8 in `trajectory_metadata_blob`) so they don't pollute the main conversation list;
3. **Longest-Prefix `project_id` Binding**: Matches nested repositories accurately to their most specific Project UUID;
4. **On-Demand `LoadTrajectory` Trigger**: Calls `LoadTrajectory` whenever a new `.db` file or step count change is detected.
