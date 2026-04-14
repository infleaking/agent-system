# aiagent-repo

`aiagent-repo` 是一个本地运行的多 agent 执行框架。它的核心目标不是只做“一次 prompt -> 一次回答”，而是把 agent、子 agent、UI、运行状态、任务委派和进度回报统一到同一套执行模型里。

当前实现的重点是：

- root agent 常驻轮询 mailbox
- UI 通过 mailbox 给 root 发送用户请求
- root 可以创建子 agent 子进程
- 子 agent 也通过 mailbox 接收任务、回报进度、等待后续控制
- session、runtime、registry、mailbox 都写在本地文件系统里，便于调试和复盘

## 框架执行思路

### 1. root agent 不是直接读终端输入

root agent 启动后会进入常驻轮询模式，而不是传统 REPL。

它的主要工作是：

- 轮询 mailbox
- 取出发给自己的消息
- claim 这条消息进入 `in_progress`
- 调用对应处理逻辑
- 处理完成后把消息标记为 `done`

这意味着 root agent 的主循环本质上是一个 mailbox-driven runner，而不是“每次用户输入都现场跑一轮大模型”。

### 2. UI 是一个 mailbox 前端

用户不直接驱动 root 的 `agent_loop`。  
UI 会把用户输入包装成 mailbox 消息，例如：

- `kind = user_request`
- `action = prompt`

root 处理完成后，会返回一封：

- `kind = agent_reply`

UI 再轮询自己的 mailbox，把 reply 显示出来。

这样做的意义是：

- root 和 child agent 共享同一种消息机制
- 用户请求、任务回报、控制消息都可以统一建模
- 不再依赖同步调用链传结果

### 3. 子 agent 是独立进程

当 root 或其他 agent 调用 `task` 时，不是同步等待一个函数返回，而是：

1. 构造 `task_brief`
2. 创建 child agent 身份
3. 发送 `task_request`
4. 启动一个新的 Python 子进程
5. 子进程进入自己的 `serve_forever()`

child agent 平时空等 mailbox，不主动调用模型。  
只有收到明确任务时才进入执行。

### 4. 所有 agent 共享同一套运行模型

无论是 root agent 还是 child agent，核心循环是一致的：

- 轮询 mailbox
- claim 消息
- 按消息类型处理
- 必要时调用模型或工具
- 产出新消息
- complete 当前消息

角色差异主要在权限，不在运行模型：

- root agent：接收用户请求、创建子 agent、汇总结果
- delegate agent：处理具体任务、必要时继续派生子任务
- UI：发送 `user_request`，等待 `agent_reply`

### 5. mailbox 是核心协调机制

mailbox 不是“附属功能”，而是整个框架的主协调层。

当前 mailbox 消息采用三态：

- `unread`
- `in_progress`
- `done`

处理原则是：

- 未读阶段只应该被调度系统发现
- 进入处理前必须显式 claim
- 只有真正完成处理后才能标记为 done

这样可以避免“读到了但其实没有真正处理”的问题。

### 6. 子任务不是同步返回，而是进度回报

子 agent 处理任务时，不要求一定一口气做完。  
当预算接近耗尽时，应该优先：

- 汇报当前进度
- 总结已完成内容
- 说明剩余工作
- 建议是否追加预算

这使得父 agent 可以把子 agent 当成一个受预算约束的执行单元，而不是无边界自治体。

### 7. 本地文件系统就是运行时数据库

每个 session 都会在 `.aiagent-sessions/<session_id>/` 下落一套完整状态：

- `session.json`
- `runtime/`
- `registry.json`
- `mailbox/`

因此可以直接通过文件检查：

- 当前有哪些 agent
- 谁在等待
- 谁失败了
- mailbox 里还有哪些消息
- 某个 agent 最后在做什么

这也是这个框架很重要的一点：  
它优先可观察、可调试、可复盘，而不是只追求“抽象优雅”。

## 目录说明

- `aiagent/agent.py`
  核心 agent 实现，包含 root/child 的运行逻辑、mailbox 处理、任务委派、子进程启动、预算提示等。

- `aiagent/runtime.py`
  session、runtime state、registry、mailbox 的底层持久化实现。

- `aiagent/ui.py`
  终端侧观察与交互入口。它通过 mailbox 给 root 发用户请求，并等待 root 回复。

- `aiagent/main.py`
  CLI 入口。

- `aiagent/tools/`
  提供文件读写、搜索、shell、todo、task、skill 等结构化工具。

- `.aiagent-sessions/`
  每次运行的 session 数据目录。

- `.tasks/`
  持久化任务数据。

- `.transcripts/`
  压缩或转录输出。

## 运行方式

推荐使用 `uv`：

```bash
uv sync
```

配置环境变量：

```bash
ANTHROPIC_API_KEY=...
```

### 查看状态

```bash
uv run python -m aiagent.main --status
```

### 启动 root agent

```bash
uv run python -m aiagent.main --interactive
```

这里的 `--interactive` 现在表示：

- 启动 root agent
- 进入 mailbox 轮询模式

不是旧式直接 REPL。

### 启动 UI

```bash
uv run python -m aiagent.main --ui
```

然后在 UI 里直接输入自然语言，UI 会：

1. 发送 `user_request`
2. 等待 `agent_reply`
3. 把 reply 打印出来

### 单次 prompt

```bash
uv run python -m aiagent.main --prompt "List the available tools"
```

这个入口仍然可用于单次调试，但它不是当前推荐的主运行模式。

### 直接调用工具

```bash
uv run python -m aiagent.main --tool describe_tools
```

## 当前设计重点

这个框架当前更关注以下能力：

- 统一的 root/child 执行模型
- mailbox 驱动的异步协调
- agent 子进程与任务委派
- 本地可观察的 runtime 状态
- 面向调试的 session 持久化

它还处于快速迭代阶段，所以 README 更强调“执行思路”和“调试路径”，而不是把接口当成完全稳定的最终产品。
