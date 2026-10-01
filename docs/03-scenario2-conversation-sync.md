# 专题三（场景 2）：同一 Project / Workspace 下不同 Conversation 的跨端双向同步

## 1. 场景定义与痛点表象

在同一个 Project / Workspace（例如 `ce-workbench`）下：
- 你在 **Jetski App (SSH)** 里新建了 **Conversation A**，聊完后打开浏览器 **Jetski Web** 进入 `ce-workbench` 项目，列表里却没有 Conversation A；
- 反过来，你在 **Jetski Web** 的 `ce-workbench` 项目下新建了 **Conversation B**，回到 **Jetski App (SSH)** 的会话历史下拉框中，也找不到 Conversation B。

---

## 2. 源码级根因剖析：为什么共用 `conversations/*.db` 却互不可见？

虽然 `jetski-hub-server`（Web 端）和 `language_server_linux_x64`（App 端）都将对话步骤保存在 `$HOME/.gemini/jetski/conversations/<conversation_id>.db` 中，但两端的**会话列表索引（Summary Store）更新机制**完全独立：

### 2.1 为什么 App 新建的会话在 Web 端看不见？
1. **缺失 Field 18 (`project_id`)**：如《专题二》所述，App 端创建的 `.db` 仅包含 `Field 7 (workspace_uris)`，不包含 `Field 18 (project_id)`；
2. **App 端不写 `conversation_summaries.db`**：Web 端侧边栏依赖 `$HOME/.gemini/jetski/conversation_summaries.db`；
3. **Web 端的 `reconcileSummariesOnce` 仅在进程启动时运行一次**：
   在 `third_party/jetski/summarystore/` 中，`jetski-hub-server` 仅在启动初始化时扫描一次 `conversations/` 目录并与 `conversation_summaries.db` 对账。在常驻运行期间，它**没有挂载 `fsnotify` 文件系统监听**，因此外部进程新创建的 `.db` 文件不会被 `jetski-hub-server` 感知。

### 2.2 为什么 Web 新建的会话在 App 端看不见？
- App 端 (`--subclient_type=ide`) 在启动和运行期**均不扫描**远端磁盘上的 `conversations/*.db` 来构建会话列表；
- 它完全依赖当前窗口绑定的 `language_server_linux_x64` 进程通过 `ExtensionServerService/PushUnifiedStateSyncUpdate`（`topicName: "trajectorySummaries"`）向本地客户端推送会话摘要。如果某条会话是由 Web 端的 `jetski-hub-server` 创建的，App 端的 `language_server_linux_x64` 内存中根本不知道这条会话的存在，自然不会推送给 App UI。

---

## 3. 核心突破口：原生 `LanguageServerService/LoadTrajectory` RPC

通过对 `third_party/jetski/cortex/cascade_manager.go` 的源码分析，我们发现两个后端进程都暴露了同一个内部 RPC 接口：

```http
POST /exa.language_server_pb.LanguageServerService/LoadTrajectory
Content-Type: application/json
Connect-Protocol-Version: 1
x-codeium-csrf-token: <process_csrf_token>

{"cascadeId": "<conversation_id>"}
```

当向目标进程发送 `LoadTrajectory({"cascadeId": cid})` 且该 `cid` **尚未加载在目标进程的内存字典 (`cascadeMap`) 中**时，目标进程会依次执行：
1. **`trajectoryStore.LoadUnsafe(ctx, cid)`**：直接从磁盘读取 `$HOME/.gemini/jetski/conversations/<cid>.db` 的元数据与全部历史步骤；
2. **`updateStateForLoadedTrajectory` $\rightarrow$ `m.updateSummary`**：
   - 在 **Web 端 (`jetski-hub-server`)**：自动将该会话写入 `conversation_summaries.db`（带上修补后的 `project_id`）并建立 FTS5 全文检索索引，同时通过 `StreamCascadeSummariesReactiveUpdates` 实时推送到浏览器侧边栏；
   - 在 **App 端 (`language_server_linux_x64`)**：自动将该会话转换为 `CascadeTrajectorySummary` 并通过 `ExtensionServerService/PushUnifiedStateSyncUpdate` 推送给本地 Electron 客户端的 `state.vscdb`，使其立即出现在 App 会话列表中！

---

## 4. 守护进程实现机制（`jetski_conv_sync_daemon.py`）

[`src/scenario1_2_daemon/jetski_conv_sync_daemon.py`](../src/scenario1_2_daemon/jetski_conv_sync_daemon.py) 每 8 秒执行一次无感巡检：

1. **零配置动态发现双端端口与 CSRF Token (`discover_servers`)**：
   - 通过 `ss -tlnp` 获取当前用户监听在 `127.0.0.1` 的所有进程端口；
   - 从 `/proc/<pid>/cmdline`、`/proc/<pid>/environ`（`ANTIGRAVITY_CSRF_TOKEN`）以及 `/tmp/jetski_hub_server.ERR` 自动提取 `jetski-hub-server` 与所有 `language_server_linux_x64 --subclient_type ide` 的实时端口和 CSRF Token。
2. **过滤 Subagent 子会话**：
   - 解析 `trajectory_metadata_blob` 的 Protobuf 字段，自动跳过带有 Field 5 或 Field 8（Subagent 父子引用）的内部子代理轨迹，避免污染主会话列表。
3. **最长前缀匹配绑定 `project_id`**：
   - 当存在嵌套路径项目（例如既有 `/home/user/git` 项目，又有 `/home/user/git/ce-workbench` 项目）时，按 `workspace_uri` 路径长度降序匹配**最精确的子目录 Project**，避免子项目会话被误挂到父目录项目下。
4. **按需增量触发 `LoadTrajectory`**：
   - 对 Web 端：当检测到新会话、`project_id` 被修正、或磁盘 `step_count` 大于 `conversation_summaries.db` 记录值时，向 `jetski-hub-server` 调用 `LoadTrajectory`；
   - 对 App 端：当检测到匹配当前 `--workspace_id` 的会话未推送或步数变化时，向对应的 `language_server_linux_x64` 调用 `LoadTrajectory`。

---

## 5. 场景二的能力边界（为什么需要场景三？）

`jetski_conv_sync_daemon.py` 完美解决了**不同会话（或另一端尚未点开过的会话）**在 8 秒内的跨端双向同步。  
但是，一旦**同一个 Conversation 在 Web 和 App 两端都被点开过一次**，场景二的 `LoadTrajectory` 就会失效——这正是《专题四（场景 3）》要解决的核心问题。
