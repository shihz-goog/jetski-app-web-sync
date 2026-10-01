# 专题一：Jetski Web (Hub) vs. Jetski App (SSH) 八大维度全景对比与双端互补工作流

## 1. 产品定位演进

- **🌐 Jetski Web (`go/jetski-hub`, `--subclient_type=hub`)**  
  面向 **Agent-First 多项目编排** 的浏览器工作台。核心优势在于常驻后台单实例 `jetski-hub-server`、笔记本休眠/断网任务不中断、多项目单页秒切、原生支持定时 Automation（Sidecar Cron）与跨会话全文检索（SQLite FTS5）。
- **💻 Jetski App (SSH / VS Code Antigravity Extension, `--subclient_type=ide`)**  
  面向 **深度手写代码与单仓库沉浸式结对编程** 的 IDE 形态。核心优势在于完整的本地 VS Code 编辑器体验、LSP 语言服务、Tab 行内代码补全以及 Buffer 内直接 Accept/Reject 代码块。

---

## 2. 八大核心维度深度对比矩阵

| 对比维度 | 🌐 Jetski Web (Hub) | 💻 Jetski App (Remote-SSH) |
| :--- | :--- | :--- |
| **1. 后台进程模型** | 常驻单实例 `jetski-hub-server`（单进程统一管理所有 Project 与会话） | 每个打开的 SSH 窗口默认单独 `spawn` 一个 `language_server_linux_x64` 进程 |
| **2. 脱离笔记本运行能力** | **极强**：依托 `loginctl enable-linger` 与 7 天凭证，笔记本合盖/断网后长耗时任务与后台子 Agent 照常运行 | **受限**：依赖活跃的 SSH 端口转发隧道与远端 Extension Host，笔记本睡眠或网络闪断易导致流式连接中断 |
| **3. 客户端与远端资源开销** | 本地仅一个浏览器标签页；远端仅一个常驻进程 | 本地运行 Electron 完整渲染引擎；远端运行 VS Code Server + Extension Host + 独立 `language_server_linux_x64` |
| **4. 顶层工作区组织范式** | **Project-First (`project_id`)**：基于 `~/.gemini/config/projects/<uuid>.json` 组织多仓库、Git 目录与 CitC 环境 | **Folder-First (`--workspace_id`)**：一个窗口绑定一个文件夹 URI，`settings_store.go` 中 `projectID` 恒为空字符串 `""` |
| **5. 会话索引 (Summary) 存储** | 远端 `~/.gemini/jetski/conversation_summaries.db`（含 `project_id` 字段与 `conversations_fts` FTS5 全文索引表） | 笔记本本地 `state.vscdb`（通过 `ExtensionServerService/PushUnifiedStateSyncUpdate` 推送 `trajectorySummaries`） |
| **6. 代码编辑与 LSP 能力** | 提供 Turn/Commit 级 Diff 审查、文件树浏览与内置终端（无复杂行内 Tab 补全） | 完整 VS Code 编辑体验、LSP (`--enable_lsp`) 符号跳转、行内 Tab 补全 |
| **7. 全局搜索与自动化** | 原生支持 `Cmd+K` 跨项目全文检索（匹配 `title`、`workspace`、`user_body`、`agent_body`）与定时 Sidecars | 仅支持按会话标题快选，不具备跨会话正文 FTS5 索引 |
| **8. 适用场景推荐** | 架构规划、多轮自动化测试与部署、长任务巡检、多项目并发管理 | 手工微调复杂代码、断点调试、逐行 Code Review |

---

## 3. 为什么「Web + App 双端混用」需要三层同步方案？

许多开发者希望 **“在 App 里写代码并发起任务 $\rightarrow$ 合上电脑用手机/浏览器在 Web 端查看实时进度或排队发下一轮指令 $\rightarrow$ 回到工位在 App 里继续改代码”**。  
要让这一工作流真正丝滑无割裂，必须依次解决以下三层问题：

1. **第一层（Project / Workspace 映射层）**：让 App 打开的 Git 文件夹与 Web 的 `Project` 自动双向对齐，消除 `outside-of-project` 孤儿会话；
2. **第二层（同项目跨端会话发现层）**：让一端新建的 Conversation 在 8 秒内自动出现在另一端对应项目的列表中；
3. **第三层（同一会话内存单脑层）**：通过 `jetski_hub_bridge` 消除双进程内存隔离，让两端共享同一个 `CascadeManager` 内存实例，实现 `RUNNING` 实时流与下一轮排队队列的 100% 同步。
