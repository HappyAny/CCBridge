# Codex App Server API 手册

这份文档记录 CC Bridge 的 Codex backend 当前依赖的 Codex `app-server` JSON-RPC 契约，用于以后升级 Codex CLI 时做对照。

这份文档只覆盖 Codex backend。Claude Code backend 通过 `cc_bridge/appserver/claude_client.py` 做兼容适配，不提供 Codex app-server JSON-RPC。

它不是完整协议规范。完整协议以本机 `codex app-server generate-json-schema --experimental` 生成的 JSON schema 为准。

## 快照

- 日期：2026-05-20
- Codex CLI：`codex-cli 0.131.0`
- Schema 生成命令：

```powershell
codex app-server generate-json-schema --experimental --out .tmp_appserver_schema_manual
```

当前关注的 feature flags：

```text
fast_mode             stable        true
goals                 experimental  false
apps                  stable        true
hooks                 stable        true
plugins               stable        true
tool_search           stable        true
remote_control        removed       false
steer                 removed       true
tui_app_server        removed       true
```

注意：`steer` 在 feature list 中显示为 removed，但 schema 仍暴露 `turn/steer`，Bridge 当前也实际使用它。

## 升级对照流程

升级 Codex 后先不要改 Bridge 代码，先生成一份新的 schema：

```powershell
cd path\to\cc-bridge
codex --version
codex features list
codex app-server generate-json-schema --experimental --out .tmp_appserver_schema_next
```

优先对照这些文件：

```text
ClientRequest.json
ClientNotification.json
ServerNotification.json
ServerRequest.json
v2/TurnStartParams.json
v2/TurnSteerParams.json
v2/TurnInterruptParams.json
v2/ThreadStartParams.json
v2/ThreadResumeParams.json
v2/ThreadForkParams.json
v2/ThreadListParams.json
v2/ThreadTurnsListParams.json
v2/ModelListParams.json
v2/ModelListResponse.json
v2/ReviewStartParams.json
v2/AppsListParams.json
v2/PluginListParams.json
v2/ListMcpServerStatusParams.json
```

重点检查：

- 方法名是否还存在，尤其是 `turn/start`、`turn/steer`、`turn/interrupt`、`thread/resume`。
- 必填字段是否变化，尤其是 `threadId`、`input`、`expectedTurnId`。
- 枚举是否变化，尤其是 `sortDirection: asc|desc`、`approvalPolicy`、`approvalsReviewer`、reasoning effort。
- 模型列表字段是否变化，尤其是 `serviceTiers`、`additionalSpeedTiers`、`supportedReasoningEfforts`。
- server request 是否新增必须响应的审批或用户输入请求。
- streaming notification 是否换名，尤其是 `item/agentMessage/delta`、`item/completed`、`turn/completed`。

## 传输层

Bridge 通过 stdio 启动 `codex app-server`，每行一个 JSON 对象。

客户端请求：

```json
{"id":1,"method":"turn/start","params":{"threadId":"...","input":[{"type":"text","text":"hi","text_elements":[]}]}}
```

客户端通知：

```json
{"method":"initialized"}
```

服务端响应：

```json
{"id":1,"result":{}}
```

服务端请求：

```json
{"id":9,"method":"item/permissions/requestApproval","params":{...}}
```

Bridge 对服务端请求用同一个 `id` 回包：

```json
{"id":9,"result":{"permissions":{},"scope":"turn"}}
```

实现位置：`cc_bridge/appserver/client.py`。

## 生命周期

启动 app-server 后，Bridge 先发送：

```json
{
  "method": "initialize",
  "params": {
    "clientInfo": {
      "name": "codex-telegram-bridge",
      "title": "CC Bridge",
      "version": "0.1.0"
    },
    "capabilities": {
      "experimentalApi": true
    }
  }
}
```

初始化成功后发送通知：

```json
{"method":"initialized"}
```

## 当前 Bridge 已接入的方法

| 方法 | Bridge 用途 | 代码位置 | 兼容性注意 |
| --- | --- | --- | --- |
| `initialize` | app-server 握手 | `appserver/client.py` | 需要 `experimentalApi: true`。 |
| `thread/list` | project/thread 列表 | `core/threads.py` | 使用 `sourceKinds`、`archived: false`、`useStateDbOnly: true` 过滤主会话。 |
| `thread/start` | `/new` 新建 thread | `core/threads.py` | Bridge 发送 `approvalPolicy: on-request`、`approvalsReviewer: auto_review`。 |
| `thread/resume` | 选择 thread 后恢复 | `core/threads.py` | 发送 `excludeTurns: true`，历史另走 `thread/turns/list`。 |
| `thread/read` | `/threadinfo` | `core/threads.py` | `includeTurns` 可选。 |
| `thread/name/set` | `/rename`，fork 后命名 | `core/threads.py` | 只改 thread 名称。 |
| `thread/archive` | `/archive` | `core/threads.py` | 归档后清当前选择。 |
| `thread/unarchive` | `/unarchive` | `core/threads.py` | 可恢复后立即选择。 |
| `thread/rollback` | `/rollback` | `core/threads.py` | 只回滚对话历史，不回滚工作区文件。 |
| `thread/compact/start` | `/compact` | `core/threads.py` | 异步压缩。 |
| `thread/turns/list` | `/history` 和恢复预览 | `core/threads.py` | `sortDirection` 必须是 `asc` 或 `desc`，不是 `descending`。 |
| `thread/fork` | `/fork` | `core/threads.py` | 尽量继承源 thread 的 model、effort、serviceTier。 |
| `thread/goal/get` | `/goal` 查询 | `core/threads.py` | goals feature 关闭时会返回错误。 |
| `thread/goal/set` | `/goal ...` 设置 | `core/threads.py` | 状态支持 `active`、`paused`、`budgetLimited`、`complete`。 |
| `thread/goal/clear` | `/goal clear` | `core/threads.py` | goals feature 关闭时会返回错误。 |
| `turn/start` | 新 turn | `core/turns.py` | Bridge 会附带 thread 级 model、effort、serviceTier。 |
| `turn/steer` | 活跃 turn 内追加输入 | `core/turns.py` | 需要 `expectedTurnId` 匹配当前活跃 turn。失败则回退排队。 |
| `turn/interrupt` | `/interrupt` | `core/turns.py` | Bridge 会清本地队列，并把已收集输出更新给 Telegram/HTTP。 |
| `model/list` | `/model`、默认模型、`/fast` | `core/models.py` | 分页读取；Bridge 用 `serviceTiers`，兼容旧 `additionalSpeedTiers`。 |
| `account/rateLimits/read` | `/limits` | `core/diagnostics.py` | 只读。 |
| `mcpServerStatus/list` | `/mcp` | `core/diagnostics.py` | detail 取 `toolsAndAuthOnly` 或 `full`。 |
| `review/start` | `/review` | `core/diagnostics.py` | 支持 inline/detached。 |
| `gitDiffToRemote` | `/diff` | `core/diagnostics.py` | 依赖 selected project cwd。 |
| `config/read` | `/config` | `core/diagnostics.py` | 可选 `includeLayers`。 |
| `configRequirements/read` | `/config` | `core/diagnostics.py` | 与 config 一起展示。 |
| `modelProvider/capabilities/read` | `/config` | `core/diagnostics.py` | 与 config 一起展示。 |
| `skills/list` | `/skills` | `core/diagnostics.py` | 参数是 `cwds: [cwd]`。 |
| `hooks/list` | `/hooks` | `core/diagnostics.py` | 参数是 `cwds: [cwd]`。 |
| `app/list` | `/apps` | `core/diagnostics.py` | 可传 `threadId`，app 可见性可能受 thread 影响。 |
| `plugin/list` | `/plugins` | `core/diagnostics.py` | 可传 `cwds`。 |
| `plugin/read` | `/plugins name` | `core/diagnostics.py` | 按 pluginName 读取详情。 |
| `getConversationSummary` | `/summary` | `core/threads.py` | 旧命名接口，不在 v2 方法清单中；升级时重点验证。 |

## 核心参数契约

### `turn/start`

当前 schema 必填：

```text
threadId
input
```

Bridge 当前发送的最小参数：

```json
{
  "threadId": "<thread-id>",
  "input": [
    {"type": "text", "text": "hello", "text_elements": []}
  ]
}
```

可选但 Bridge 会使用的字段：

```text
model
effort
serviceTier
```

schema 中存在但 Bridge 当前不主动设置的高风险字段：

```text
approvalPolicy
approvalsReviewer
collaborationMode
cwd
permissions
personality
runtimeWorkspaceRoots
sandboxPolicy
responsesapiClientMetadata
outputSchema
summary
environments
```

兼容性策略：

- 如果 `serviceTier` 不支持，Bridge 清掉该 thread 的 service tier 后重试。
- 如果 `model` 不支持，Bridge 清掉该 thread 的 model settings 后用 app-server 默认模型重试。
- `serviceTier` 是“本 turn 以及后续 turns”的覆盖字段，因此 Bridge 只在用户显式 `/fast on` 后发送 `fast`。

### `turn/steer`

当前 schema 必填：

```text
threadId
expectedTurnId
input
```

Bridge 行为：

- Telegram 普通消息默认优先 steer 到目标 thread 的活跃 turn。
- HTTP `POST /message` 默认 `steer: true`。
- `/queue` 和 `POST /queue` 不走 steer。
- 如果 steer 失败，Telegram 侧回退为排队新 turn；HTTP 侧返回 steer 结果或按普通 turn 逻辑继续。

### `turn/interrupt`

当前 schema 必填：

```text
threadId
turnId
```

Bridge 行为：

- 调 app-server 原始 `turn/interrupt`。
- 同时清掉目标 thread 的本地等待队列。
- Telegram 侧会把当前已收集输出做最终编辑。
- 不回滚 thread 历史，不回滚文件系统。

### `thread/start`

Bridge 当前发送：

```json
{
  "cwd": "<project-cwd>",
  "approvalPolicy": "on-request",
  "approvalsReviewer": "auto_review",
  "experimentalRawEvents": false,
  "persistExtendedHistory": false
}
```

升级注意：

- `persistExtendedHistory` 在 0.131.0 schema 中已经标注 deprecated/ignored，但 Bridge 仍发送以兼容旧版本。
- `serviceTier`、`model`、`runtimeWorkspaceRoots`、`permissions`、`sandbox` 都存在于 schema，但 Bridge 新建 thread 时暂不发送。

### `thread/resume`

Bridge 当前发送：

```json
{
  "threadId": "<thread-id>",
  "cwd": "<project-cwd>",
  "approvalPolicy": "on-request",
  "approvalsReviewer": "auto_review",
  "excludeTurns": true,
  "persistExtendedHistory": false
}
```

恢复后 Bridge 会单独调用 `thread/turns/list` 发送最近一轮历史预览。

### `thread/fork`

当前 schema 必填：

```text
threadId
```

Bridge 当前发送：

```json
{
  "threadId": "<source-thread-id>",
  "cwd": "<target-cwd-or-null>",
  "approvalPolicy": "on-request",
  "approvalsReviewer": "auto_review",
  "excludeTurns": true,
  "persistExtendedHistory": false,
  "model": "<optional>",
  "effort": "<optional>",
  "serviceTier": "<optional>"
}
```

兼容性注意：

- schema 说明 `path` 可覆盖 `threadId`，但 Bridge 默认不走 path。
- Fork 成功后如果用户提供 name，Bridge 再调用 `thread/name/set`。

### `thread/list`

Bridge 用它构造 project 和 thread 选择器。

Project 列表参数：

```json
{
  "limit": 200,
  "sourceKinds": ["cli", "vscode", "exec", "appServer"],
  "archived": false,
  "useStateDbOnly": true
}
```

Thread 列表参数：

```json
{
  "limit": 80,
  "cwd": "<project-cwd>",
  "sourceKinds": ["cli", "vscode", "exec", "appServer"],
  "archived": false,
  "useStateDbOnly": true
}
```

`sourceKinds` 用于隐藏 sub-agent、review、compact 等内部 thread。升级后如果列表异常，优先检查 `ThreadListParams` 和返回里的 `source` 字段。

### `thread/turns/list`

Bridge 当前发送：

```json
{
  "threadId": "<thread-id>",
  "limit": 5,
  "sortDirection": "desc",
  "itemsView": "full"
}
```

兼容性注意：

- `sortDirection` 枚举是 `asc` 和 `desc`。
- `itemsView` 可取 summary/full 等 schema 定义值；Bridge 需要 full 才能格式化历史文本。
- 有 `nextCursor` 时继续分页。

### `model/list`

Bridge 当前发送：

```json
{"includeHidden": false, "limit": 100}
```

有 `nextCursor` 时继续分页。

Bridge 依赖的返回字段：

```text
model 或 id
displayName
hidden
defaultReasoningEffort
supportedReasoningEfforts
serviceTiers
additionalSpeedTiers
nextCursor
```

兼容性策略：

- 默认模型按名称打分选择“最好模型”，再选该模型支持的最大 effort。
- `serviceTiers` 是新字段。
- `additionalSpeedTiers` 在 schema 中标注 deprecated，Bridge 仍作为 fallback。
- `/fast on` 只有在当前模型 tier 列表包含 `fast` 时才允许。

## 输入项

Bridge 当前会向 `turn/start` 和 `turn/steer` 发送这些 `UserInput`：

文本：

```json
{"type":"text","text":"hello","text_elements":[]}
```

Telegram 图片：

```json
{"type":"localImage","path":"D:\\Codes\\Vibe\\cc-bridge\\downloads\\..."}
```

Telegram 文件：

```json
{"type":"mention","name":"file.txt","path":"D:\\Codes\\Vibe\\cc-bridge\\downloads\\file.txt"}
```

HTTP 调用也可以直接传 `items`，Bridge 不会重写 item 结构，只做基本 list/object 校验。

## 流式事件

Bridge 只把带 `turnId` 的通知路由到对应活跃 turn。

当前必须稳定的通知：

| 通知 | Bridge 用途 |
| --- | --- |
| `item/agentMessage/delta` | 追加流式文本，并按节流编辑 Telegram placeholder。 |
| `item/completed` | 如果完成项是 agent message，用完整 text 覆盖增量文本。 |
| `turn/completed` | 结束 turn，发送最终文本或错误文本。 |

当前会缓存但不消费的常见通知：

```text
turn/started
item/started
turn/diff/updated
turn/plan/updated
item/plan/delta
item/reasoning/summaryTextDelta
item/reasoning/summaryPartAdded
item/reasoning/textDelta
hook/started
hook/completed
```

升级风险：

- 如果 agent 文本 delta 改名，Telegram 实时刷新会失效。
- 如果最终文本不再出现在 `turn/completed.turn` 或 `item/completed.item.text`，HTTP 同步响应和 Telegram 最终输出会空。
- 如果 notification 不再携带 `turnId` 或 `turn.id`，Bridge 无法按多 thread 并行路由。

## 服务端请求

app-server 可能反向请求 Bridge 做审批、用户输入或工具调用。Bridge 当前处理策略：

| 服务端请求 | Bridge 响应 | 代码位置 |
| --- | --- | --- |
| `item/commandExecution/requestApproval` | Telegram buttons: `accept` / `acceptForSession` / `decline`; `/interrupt` declines pending approvals for the target thread before calling `turn/interrupt` | `appserver/events.py` |
| `item/fileChange/requestApproval` | Telegram buttons: `accept` / `acceptForSession` / `decline`; `/interrupt` declines pending approvals for the target thread before calling `turn/interrupt` | `appserver/events.py` |
| `item/permissions/requestApproval` | Telegram buttons: requested permissions with `turn` / `session` scope, or empty permissions on deny | `appserver/events.py` |
| `item/tool/requestUserInput` | `{"answers":{}}` | `appserver/events.py` |
| 其他未知请求 | JSON-RPC error `-32601` | `appserver/events.py` |

如果 Telegram chat 尚未绑定，或审批消息发送失败，Bridge 会安全拒绝审批请求，避免 app-server turn 无限等待。审批消息会对常见 token、Bearer、password、secret、API key 字段做发送前脱敏。未处理的审批 10 分钟后自动安全拒绝。`/approvals` 可查看或批量拒绝待处理审批。审批结果写入 `approval_audit.log`，只记录 method、request id、thread/turn id、chat/actor 等元数据，不记录命令正文或权限 payload。

当前 schema 里还存在这些 server request，Bridge 尚未专项处理：

```text
mcpServer/elicitation/request
item/tool/call
account/chatgptAuthTokens/refresh
attestation/generate
applyPatchApproval
execCommandApproval
```

升级后如果 Codex 开始依赖这些请求，可能出现 turn 卡住、工具失败或认证刷新失败。

## 当前 schema 方法索引

以下是 0.131.0 schema 里的 ClientRequest 方法清单。Bridge 只接入上文表格中的子集。

Thread：

```text
thread/start
thread/resume
thread/fork
thread/archive
thread/unsubscribe
thread/increment_elicitation
thread/decrement_elicitation
thread/name/set
thread/goal/set
thread/goal/get
thread/goal/clear
thread/metadata/update
thread/memoryMode/set
thread/unarchive
thread/compact/start
thread/shellCommand
thread/approveGuardianDeniedAction
thread/backgroundTerminals/clean
thread/rollback
thread/list
thread/loaded/list
thread/read
thread/turns/list
thread/turns/items/list
thread/inject_items
```

Turn：

```text
turn/start
turn/steer
turn/interrupt
```

Realtime：

```text
thread/realtime/start
thread/realtime/appendAudio
thread/realtime/appendText
thread/realtime/stop
thread/realtime/listVoices
```

Models、config、features：

```text
model/list
modelProvider/capabilities/read
collaborationMode/list
experimentalFeature/list
experimentalFeature/enablement/set
config/read
config/value/write
config/batchWrite
configRequirements/read
```

Skills、hooks、apps、plugins：

```text
skills/list
skills/config/write
hooks/list
app/list
plugin/list
plugin/read
plugin/skill/read
plugin/install
plugin/uninstall
plugin/share/save
plugin/share/updateTargets
plugin/share/list
plugin/share/checkout
plugin/share/delete
marketplace/add
marketplace/remove
marketplace/upgrade
```

MCP：

```text
mcpServer/oauth/login
config/mcpServer/reload
mcpServerStatus/list
mcpServer/resource/read
mcpServer/tool/call
```

Filesystem、command、process：

```text
fs/readFile
fs/writeFile
fs/createDirectory
fs/getMetadata
fs/readDirectory
fs/remove
fs/copy
fs/watch
fs/unwatch
command/exec
command/exec/write
command/exec/terminate
command/exec/resize
process/spawn
process/writeStdin
process/kill
process/resizePty
```

Account、remote control、review、misc：

```text
account/read
account/login/start
account/login/cancel
account/logout
account/rateLimits/read
account/sendAddCreditsNudgeEmail
remoteControl/enable
remoteControl/disable
remoteControl/status/read
review/start
memory/reset
environment/add
feedback/upload
externalAgentConfig/detect
externalAgentConfig/import
windowsSandbox/setupStart
windowsSandbox/readiness
fuzzyFileSearch
fuzzyFileSearch/sessionStart
fuzzyFileSearch/sessionUpdate
fuzzyFileSearch/sessionStop
mock/experimentalMethod
initialize
```

## Feature 备注

Goals：

- `thread/goal/*` 方法存在。
- 当前 feature flag 是 `goals experimental false`。
- 未开启时 app-server 会返回类似 `goals feature is disabled`。
- 可用 `codex features enable goals` 或在 `~/.codex/config.toml` 设置 `[features] goals = true` 后重启 Bridge。

Fast：

- `fast_mode stable true`。
- `serviceTier` 字段存在于 `turn/start`、`thread/start`、`thread/resume`、`thread/fork`。
- Bridge 默认不发送 `serviceTier`。
- `/fast on` 后才对目标 thread 发送 `serviceTier: "fast"`。

Remote control：

- schema 中有 `remoteControl/enable`、`remoteControl/disable`、`remoteControl/status/read`。
- feature list 中 `remote_control removed false`。
- Bridge 当前不接入 remote control；外部控制使用 Bridge 自己的 HTTP Control API。

Apps、hooks、plugins：

- feature flag 当前都是 stable true。
- Bridge 已提供只读列举接口。
- 写操作如 plugin install/uninstall、skills config write、marketplace add/remove 当前不开放给 Telegram/HTTP。

## Bridge HTTP API 与 app-server API 的关系

Bridge 暴露的 HTTP Control API 是给外部程序用的稳定外壳，不等同于 app-server 原始 JSON-RPC。

例子：

- `POST /message` 最终可能调用 `turn/steer` 或 `turn/start`。
- `POST /queue` 固定排队后调用 `turn/start`。
- `POST /interrupt` 调用 `turn/interrupt`，并额外清 Bridge 本地队列。
- `GET /models` 调用 `model/list`，并附带 Bridge 当前 thread 的已选 model/effort/serviceTier。
- `POST /fast` 不直接调用 app-server，只修改 Bridge 保存的 thread settings；下一次 `turn/start` 才体现。
- `POST /auth/switch` 不属于 app-server API；它停止 app-server、替换 `auth.json`、再重启。

HTTP API 清单见 `README.md` 的 `HTTP Control API` 小节。

## 安全边界

Bridge 当前安全策略：

- Telegram 只响应 `ALLOWED_TELEGRAM_CHAT_IDS` 清单中的 chat id。
- HTTP 默认绑定 `127.0.0.1`。
- HTTP 绑定非 loopback 地址时必须配置 `HTTP_CONTROL_TOKEN`。
- app-server 反向审批请求默认取消或给空权限，不让 Telegram/HTTP 直接批准危险动作。
- `/switch` 切换账户时如果有活跃 turn，会拒绝切换。

升级后重点确认：

- `approvalsReviewer: auto_review` 是否仍被 schema 接受。
- `approvalPolicy: on-request` 是否仍被 schema 接受。
- 新增 server request 是否会绕过当前取消策略。
- 文件、命令、process 类 app-server API 是否被无意暴露到 Bridge HTTP route。

## 已知兼容性坑

- `thread/turns/list.sortDirection` 是 `desc`，不是 `descending`。
- `getConversationSummary` 是旧命名接口，当前 Bridge 仍使用；升级时优先验证。
- `turn/steer` 在 feature list 中显示 removed，但 schema 当前仍有；如果未来移除，默认消息路由要改成排队。
- `persistExtendedHistory` 已 deprecated/ignored，但 Bridge 为兼容旧版本仍发送。
- `additionalSpeedTiers` 已 deprecated，Bridge 只把它当 `serviceTiers` 的 fallback。
- `serviceTier` 是 sticky override，不应该默认开启 fast。
- goals 方法存在不代表可用，受 feature flag 控制。
