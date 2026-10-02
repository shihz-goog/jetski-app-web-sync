<div align="center">

# Topic 1: Jetski Web (Hub) vs. Jetski App (SSH) — 8-Dimension Comparison & Dual-Client Workflow

**[🇨🇳 简体中文](../01-web-vs-app-comparison.md) | [🌐 English](./01-web-vs-app-comparison.md) | [🏠 Home (README)](../../README_EN.md)**

</div>

---

## 1. Product Positioning

- **🌐 Jetski Web (`go/jetski-hub`, `--subclient_type=hub`)**  
  An **Agent-First multi-project orchestration** browser workbench. Its core strengths include a persistent single-instance `jetski-hub-server` daemon, uninterrupted execution when your laptop sleeps or disconnects, instant multi-project switching, native scheduled Automations (Sidecar Cron), and cross-conversation full-text search (SQLite FTS5).
- **💻 Jetski App (SSH / VS Code Antigravity Extension, `--subclient_type=ide`)**  
  An IDE tailored for **deep hands-on coding and single-repo immersive pair programming**. Its core strengths include a full native VS Code editor experience, LSP language services, inline Tab completions, and direct in-buffer Accept/Reject code hunks.

---

## 2. 8-Dimension Deep Comparison Matrix

| Dimension | 🌐 Jetski Web (Hub) | 💻 Jetski App (Remote-SSH) |
| :--- | :--- | :--- |
| **1. Backend Process Model** | Persistent single-instance `jetski-hub-server` managing all Projects and conversations in one process | Each open SSH window spawns a dedicated `language_server_linux_x64` process by default |
| **2. Laptop-Detached Execution** | **Excellent**: Powered by `loginctl enable-linger` and 7-day credentials; long tasks and subagents keep running when the laptop lid is closed | **Limited**: Depends on an active SSH port-forwarding tunnel and Remote Extension Host; sleep or network drops interrupt live streams |
| **3. Client & Remote Resource Footprint** | Single browser tab locally; single persistent daemon remotely | Full Electron renderer locally; VS Code Server + Extension Host + dedicated `language_server_linux_x64` remotely |
| **4. Top-Level Workspace Paradigm** | **Project-First (`project_id`)**: Organizes multi-repo folders and CitC workspaces via `~/.gemini/config/projects/<uuid>.json` | **Folder-First (`--workspace_id`)**: Binds one window to one folder URI; `projectID` in `settings_store.go` is always `""` |
| **5. Conversation Summary Store** | Remote `~/.gemini/jetski/conversation_summaries.db` (with `project_id` column and `conversations_fts` FTS5 full-text index) | Laptop-local `state.vscdb` (populated via `ExtensionServerService/PushUnifiedStateSyncUpdate` on topic `trajectorySummaries`) |
| **6. Code Editing & LSP** | Turn/Commit-level Diff review, file tree browsing, and integrated terminal (no inline Tab completions) | Full VS Code editing, LSP (`--enable_lsp`) symbol navigation, and inline Tab completions |
| **7. Global Search & Automation** | Native `Cmd+K` cross-project full-text search (matching `title`, `workspace`, `user_body`, `agent_body`) & scheduled Sidecars | Quick-pick by conversation title only; no cross-conversation FTS5 body index |
| **8. Recommended Use Cases** | Architecture planning, multi-turn automated testing & deployment, long-running task monitoring, multi-project orchestration | Fine-grained manual coding, interactive debugging, line-by-line Code Review |

---

## 3. Why Combining Web + App Requires a 3-Tier Sync Solution

Many developers want a seamless workflow: **"Write code and kick off a task in App $\rightarrow$ close the laptop lid and monitor live progress or queue the next instruction on Web $\rightarrow$ return to the desk and continue editing in App."**  
To make this workflow truly frictionless, three progressive layers must be solved:

1. **Tier 1 (Project / Workspace Mapping)**: Automatically align Git folders opened in App with `Projects` in Web, rescuing orphan conversations from `outside-of-project`;
2. **Tier 2 (Cross-Client Conversation Discovery)**: Make conversations created in one client appear in the peer client's project list within 8 seconds;
3. **Tier 3 (Same-Conversation Single-Brain Memory)**: Eliminate dual-process RAM isolation via `jetski_hub_bridge` so both clients share the exact same `CascadeManager` instance for `RUNNING` live streams and `QueuedSteps`.
