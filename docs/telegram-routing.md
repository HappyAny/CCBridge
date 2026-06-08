# Telegram 路由与 Thread 上下文

这份文档记录 CC Bridge 把 Telegram 消息路由到当前 backend thread/session 时使用的运行规则。Codex backend 使用 Codex app-server thread；Claude backend 使用 Claude Code session 兼容层。

## 启动上下文

Bridge 启动后不会主动向 Telegram 发送 project/thread 选择消息。它只会加载本地状态、启动当前 backend，然后等待 Telegram 输入。

默认 backend 是 Codex。可以用 `/backend` 查看当前 backend，用 `/codex` 或 `/claude` 切换。backend 切换会保存当前 backend 的 project/thread/model 状态，并恢复目标 backend 自己的状态。

如果默认 backend 启动失败，manager bot 仍会继续启动，让用户可以在 Telegram 里切换或重试 backend。

在 Bridge 进入 `ready` 状态并且已经选中 thread 之前，普通 Telegram 文本不会转发给 backend，而是触发 project/thread 选择流程。

未选择 thread 前仍然可用的全局命令：

```text
/start
/project
/thread
/help
/status
/backend
/codex
/claude
/stop
/limits
/mcp
/switch
/archive
```

其他命令都需要 thread 上下文，例如 `/model`、`/queue`、`/interrupt`、`/history`、`/diff`、`/config`、`/skills`、`/hooks`、`/fork`、`/apps`。

`/switch` 是全局命令，因为 Codex 账户切换不依赖当前 project/thread。它只适用于 Codex backend；在 Claude backend 下会提示先 `/codex`。切换时如果 backend 正在回复，会拒绝切换并提示先 `/interrupt`。

## Project 和 Thread 列表

Project 不是单独的注册表，而是由当前 backend 的 thread/session 按 `cwd` 分组得到。

Telegram 里的 project/thread 选择器只显示主会话，使用 `VISIBLE_SOURCE_KINDS` 过滤：

```python
["cli", "vscode", "exec", "appServer"]
```

`subAgent`、`subAgentReview`、`subAgentCompact`、`subAgentThreadSpawn` 等内部来源会从 `/project` 和 `/thread` 中隐藏。

`/project` 中显示的 thread 数量会用同一套可见 thread 列表计算，所以它应该和 `/thread` 实际可选的数量一致。

## 默认路由

已经选中 thread 且没有 Telegram 回复绑定时，普通 Telegram 文本会发送到当前聚焦 thread。

如果目标 thread 有正在运行的 turn，Bridge 会先尝试 `turn/steer`。如果 steer 失败，再退回为排队新 turn。

使用 `/queue message` 可以强制排队，不走 steer。

使用 `/interrupt` 会中断目标 thread 当前活跃 turn，并清掉同一个 thread 的本地待处理队列。

## Telegram 回复路由

当用户在 Telegram 里回复一条旧的 bot 消息时，Bridge 会用这条 Telegram 消息 id 去当前 backend 状态里的 `telegram_message_bindings` 中查找绑定。

如果能找到绑定，新的消息或命令会路由回那条 bot 消息原本所属的 thread，即使当前聚焦 thread 已经切到了别处。

这条规则在 Bridge 重启后仍然有效，因为绑定会持久化在 `state.json` 中。

回复路由失效的情况：

- `state.json` 被删除或替换。
- 对应 Telegram 消息绑定被清理。
- 用户回复的消息不是 Bridge 发送并记录过的 bot 消息。

## 新 Turn 的 Telegram 回复关系

对于非 steer 的新 turn，Bridge 创建 placeholder 消息时会使用 Telegram 原生回复关系，回复到触发这次 turn 的用户消息。

这样在 Telegram 聊天里可以直接看出某条 Codex 回复是针对哪条用户消息开的新 turn。

流式输出会持续编辑这个 placeholder 消息。最终输出太长时，会拆成额外 Telegram 消息发送。

## 多 Thread 运行

每个 thread 有独立队列和运行锁。不同 thread 可以同时运行；同一个 thread 内部仍然串行执行。

切换当前聚焦 thread 不会停止旧 thread 里正在运行的 turn。旧 thread 的输出会继续更新它原来的 Telegram bot 消息。

## Model 和 Effort

模型和推理强度是 thread 级别配置。

Bridge 会把配置保存在 `state.json`：

```json
{
  "thread_model_settings": {
    "<threadId>": {
      "model": "gpt-5.5",
      "effort": "xhigh",
      "source": "thread",
      "updatedAt": 1779213318.161257
    }
  }
}
```

如果某个 thread 没有保存过模型配置，Bridge 会从当前 backend 实时读取模型列表，选择可用的最好模型和最大推理强度，并把结果保存到该 thread。Fast service tier 默认关闭，不会因为模型支持 `fast` 就自动开启。

`/model` 会修改当前目标 thread 的模型配置。如果 `/model` 是作为某条旧 Codex bot 消息的 Telegram 回复发出的，它会修改那条旧消息所属 thread，而不是当前聚焦 thread。

Fork 出来的 thread 会尽量继承源 thread 的模型和推理强度。

`/fast` 控制当前目标 thread 的 `serviceTier`。使用 `/fast on` 后，后续 `turn/start` 会带上 `serviceTier: "fast"`；使用 `/fast off` 会清掉该字段并回到 app-server 默认 service tier。只有当当前模型从 `model/list` 报告支持 `fast` tier 时才能开启。

## 配置作用域

Bridge 里真正 thread 级别的内容包括：历史、摘要、名称、归档、回滚、compact、goal、fork、turn 运行态、模型/推理强度、Telegram 消息绑定。

Skills、hooks、大部分 MCP 状态不是 thread 级别，而是跟当前 backend 配置和 project `cwd` 相关。Codex backend 直接转发到 app-server；Claude backend 只支持兼容层已经实现的能力。

Apps 比较特殊：app 列表接口可以接受 `threadId`，因此 app 可见性可能受 thread 上下文影响。
