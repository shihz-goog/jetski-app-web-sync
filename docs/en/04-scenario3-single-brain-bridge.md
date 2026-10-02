<div align="center">

# Topic 4 (Scenario 3): Same-Conversation Multi-Turn & Live Task (`RUNNING` + Queue) Single-Brain Synchronization

**[🇨🇳 简体中文](../04-scenario3-single-brain-bridge.md) | [🌐 English](./04-scenario3-single-brain-bridge.md) | [🏠 Home (README)](../../README_EN.md)**

</div>

---

## 1. Scenario Definition & Three Critical Split-Brain Failures

When switching back and forth between **Jetski App (SSH)** and **Jetski Web** inside the **exact same conversation (`conversation_id`)**, three severe issues occur under the default dual-process architecture:

1. **Subsequent turns stop syncing**: Once a conversation has been opened on both Web and App, continuing for 10 more turns in App (e.g., from Step 640 to Step 1169) leaves Web permanently frozen at Step 640—even if `jetski_conv_sync_daemon.py` calls `LoadTrajectory`;
2. **Live task execution (`RUNNING`) and streaming output are invisible**: While a long-running task is actively streaming tool calls and thoughts in App (`CASCADE_RUN_STATUS_RUNNING`), opening the same conversation in Web shows `IDLE` with zero live output;
3. **Cross-client message queuing fails and triggers SQLite write collisions**: If App is currently executing at Step 1169 (`RUNNING`) and you send a follow-up message in Web (expecting it to enter `QueuedSteps`), Web believes the conversation is `IDLE` at Step 640 and immediately starts executing from `idx = 641`, running `INSERT OR REPLACE INTO steps (idx=641...)` and **silently overwriting App's Steps 641+ on disk!**

---

## 2. Source-Level Root Causes

![Native Split-Brain Architecture (EN)](../assets/scenario2_3_dual_brain_split_en.jpg)

### Root Cause 1: Hardcoded `checkAlreadyLoaded` Guard in `cascade_manager.go:1454`
In `third_party/jetski/cortex/cascade_manager.go`, the very first check in `LoadTrajectory` short-circuits if the conversation is already in RAM:

```go
func (m *CascadeManager) LoadTrajectory(ctx context.Context, cascadeID string) error {
    if m.checkAlreadyLoaded(ctx, cascadeID) {
        return nil // ⚠️ Returns nil immediately if cascadeID is already in cascadeMap RAM — never reloads from disk!
    }
    // ...
}
```
Across all 320 RPCs in `LanguageServerService`, **no RPC exists to evict a single conversation from `cascadeMap` or force a disk reload**. Once both processes load `cid` into their own `cascadeMap`, each process serves only its own in-memory snapshot forever.

### Root Cause 2: `RUNNING` State, Live Streams, and `QueuedSteps` Reside 100% in Single-Process RAM
- **Live Streaming (`StreamAgentStateUpdates`)**: Subscribes to Go channels inside a specific process's `CascadeManager`;
- **Message Queuing (`SendUserCascadeMessage` $\rightarrow$ `QueuedSteps`)**: A new message is appended to `QueuedSteps` if and only if the receiving process's in-memory status is `CASCADE_RUN_STATUS_RUNNING`.

### Root Cause 3: SQLite `steps` Table Uses `idx` as `PRIMARY KEY`
```sql
CREATE TABLE `steps` (
    `idx` integer,
    `step_type` integer,
    `data` blob,
    PRIMARY KEY (`idx`)
);
```
Because `idx` is incremented from each process's own in-memory counter, sending a message on the lagging client overwrites existing rows via `INSERT OR REPLACE`.

---

## 3. Ultimate Solution: `jetski_hub_bridge` Unified Single-Brain Bridge

![Unified Single-Brain Bridge Architecture (EN)](../assets/scenario3_single_brain_bridge_en.jpg)

Because **Jetski App (SSH)**'s Remote Extension Host (`extension.js`) and **Jetski Web**'s `jetski-hub-server` run on the **exact same Cloudtop** and speak the exact same 320 `LanguageServerService` RPCs, the definitive solution is to **eliminate the second brain and route Jetski App (SSH) directly into `jetski-hub-server`!**

### 3.1 Why App Cannot Connect to `jetski-hub-server`'s Port Directly
Reverse-engineering `~/.jetski-server/bin/.../extensions/antigravity/dist/extension.js` (modules `32266`, `17689`, `55562`) reveals three barriers:
1. **TLS Certificate Pinning**: `extension.js` (module `55562`) and the local Electron client verify `https://127.0.0.1:<httpsPort>` strictly against the bundled `dist/languageServer/cert.pem` (`SHA1: E037E5905BE4DD4A43E8F66907410ADC75AFAA95`), whereas `jetski-hub-server` uses a dynamically generated self-signed cert;
2. **CSRF Token Mismatch**: `extension.js` sends its own `x-codeium-csrf-token`, which must be rewritten to match `jetski-hub-server`'s token;
3. **`Exit` RPC Protection & `ReconnectExtensionServer` Handling**: Closing an SSH window sends `/LanguageServerService/Exit` (which must be intercepted so Web's server isn't killed), and `/ReconnectExtensionServer` must be answered directly because `jetski-hub-server` in `--mode hub` has no `ExtensionServerClient`.

### 3.2 How `jetski_hub_bridge` Achieves Zero-Reload Live Attach

Implemented in [`src/scenario3_bridge/jetski_hub_bridge.go`](../../src/scenario3_bridge/jetski_hub_bridge.go):
1. **Native Hot Discovery (`antigravity.persistentLanguageServer`) + `jetski_ls_shim.py`**:
   - `extension.js` runs `maybeUpdate()` every 1 second checking `antigravity.persistentLanguageServer` and `~/.gemini/jetski/daemon/ls_<sha256(workspaceId)[:16]>.json`;
   - `enable_unified_bridge.py` writes the exact `lsVersion` prefix matching `~/.jetski-server/bin/<version>-<commit>` and configures [`jetski_ls_shim.py`](../../src/scenario3_bridge/jetski_ls_shim.py), allowing `extension.js` to attach to `:37999` within 1 second **without reloading the window**.
2. **Dynamic Local Certificate Extraction (Zero Hardcoded Keys)**:
   - [`extract_local_cert.py`](../../src/scenario3_bridge/extract_local_cert.py) extracts the embedded private key and `cert.pem` from the local `language_server_linux_x64` binary at install time (gitignored).
3. **Zero-Latency HTTP/2 Streaming Proxy (`FlushInterval: -1`) & CSRF Rewriting**:
   - Runs as a `systemd --user` service ([`jetski-hub-bridge.service`](../../src/scenario3_bridge/jetski-hub-bridge.service)), auto-discovering `jetski-hub-server`'s HTTPS port and CSRF token and streaming all RPCs with `FlushInterval: -1`.
4. **Direct `ReconnectExtensionServer` ACK & 2-Second USS Sidebar Push**:
   - Intercepts `/ReconnectExtensionServer` with `200 OK {}` and pushes updated summaries from `conversation_summaries.db` to App's `ExtensionServerService/PushUnifiedStateSyncUpdate` every 2 seconds.
