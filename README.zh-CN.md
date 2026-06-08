# CC Bridge

[English](README.md)

CC Bridge 是一个本地运行的 Telegram 控制入口，用同一个 Telegram Bot 控制 Codex 和 Claude Code。

它会在你的机器上启动服务，监听 Telegram 消息，并把消息转发到 Codex `app-server` thread 或 Claude Code session。你可以在 Telegram 里切换后端、选择项目和线程、流式接收回复、审批工具调用、中断卡住的任务、上传文件、取回生成的图片，也可以通过本地 HTTP API 调用同一套能力。

默认后端是 Codex。可以在 Telegram 里用 `/backend`、`/codex`、`/claude` 切换。

## 功能

- 一个 Telegram Bot 同时管理 Codex 和 Claude Code。
- Codex 后端通过 JSON-RPC stdio 连接 `codex app-server`。
- Claude 后端调用本地 `claude` CLI，并尽量适配成同样的桥接流程。
- Telegram 内支持项目、线程、模型、fast tier、历史、fork、归档、回滚、compact、summary 和 goal 命令。
- 支持 Telegram 文本、图片和文件上传。最近一轮生成的图片和文件可以用 `/images`、`/files` 列出并发送回 Telegram。
- Codex 需要审批命令、文件改动或权限时，Bridge 会在 Telegram 里发送 inline 按钮。
- `/interrupt` 会拒绝目标线程的待审批请求，中断当前后端 turn，并清空尚未开始的队列消息。
- 本地 HTTP 控制服务可供脚本、桌面工具或其他自动化入口调用。
- 提供 Windows、macOS、Linux tray 启动脚本。
- 运行状态、日志、下载文件、本地配置、锁文件和认证文件默认不会进 git。

## 环境要求

- Python 3.10 或更新版本。
- 通过 BotFather 创建的 Telegram Bot token。
- 至少一个允许访问的 Telegram chat id。
- 如果使用 Codex 后端，需要本机可运行 `codex app-server`。
- 如果使用 Claude 后端，需要本机可运行 Claude Code CLI。

Python 依赖见 [`requirements.txt`](requirements.txt)。

## 快速开始

```powershell
cd path\to\cc-bridge
python -m pip install -r requirements.txt
copy config.local.example.py config.local.py
```

编辑 `config.local.py`：

```python
BOT_TOKEN = "telegram-bot-token"

ALLOWED_TELEGRAM_CHAT_IDS = [
    123456789,
]
```

启动：

```powershell
python -m cc_bridge
```

然后在 Telegram 里向 Bot 发送 `/start` 或 `/project`。

## 检查命令

只检查本地配置和后端启动，不启动 Telegram polling：

```powershell
python -m cc_bridge --check
python -m cc_bridge --check --backend claude
```

运行诊断，不启动 Telegram polling：

```powershell
python -m cc_bridge --doctor
python -m cc_bridge --doctor --backend claude
```

运行测试：

```powershell
python -B -m unittest discover -s tests
```

## Tray 模式

按平台使用对应启动器：

- Windows：双击 `CC Bridge.bat`。
- macOS：运行 `start_tray.command`。
- Linux：运行 `start_tray.sh`。

Windows 启动器会调用 `run_tray.vbs`，用 `pythonw.exe -m cc_bridge.tray` 静默启动，不留下终端窗口。启动时它会停止旧的 `cc_bridge`、`codex_telegram_bridge`、`claudecode_telegram_bridge` Python tray 进程，避免同一个 Telegram Bot token 被多个 `getUpdates` 消费者抢占。

CC Bridge 运行时还会持有 `cc_bridge.lock` 单实例锁。如果崩溃后锁文件还在，先确认没有 CC Bridge 进程在运行，再删除它。

Tray 日志写入 `bridge.log`。启动时如果日志超过 512 KB，会轮转为 `bridge.log.1`，最多保留 3 份备份。`approval_audit.log` 在写入审批审计记录前也使用同样的轮转策略。

## 配置

`config.local.py` 是必需文件，但不会进入 git。建议从 [`config.local.example.py`](config.local.example.py) 复制：

```python
BOT_TOKEN = "telegram-bot-token"

# 可选。如果使用 ALLOWED_TELEGRAM_CHAT_IDS，可以保持为空。
TELEGRAM_CHAT_ID = None

# 必填。只有这些 Telegram chat 可以控制 Bridge。
ALLOWED_TELEGRAM_CHAT_IDS = [
    123456789,
]

# 可选，本地 HTTP 控制 API。
HTTP_CONTROL_HOST = "127.0.0.1"
HTTP_CONTROL_PORT = 8765
HTTP_CONTROL_TOKEN = ""
```

Bridge 会忽略不在允许清单里的 Telegram update。HTTP 服务默认只绑定 localhost。如果改成非 loopback 地址，必须设置 `HTTP_CONTROL_TOKEN`。

## 后端切换

同一时间只有一个后端处于活跃状态。Telegram Bot token 和允许访问的 chat id 由 manager bot 共享，不会随后端切换而改变。

```text
/backend          查看当前后端和切换提示
/backend codex    切到 Codex
/backend claude   切到 Claude Code
/codex            /backend codex 的快捷命令
/claude           /backend claude 的快捷命令
```

如果当前有活跃 turn 或队列消息，切换会被拒绝。先用 `/interrupt` 停掉当前工作。

如果后端启动失败，CC Bridge 会保持 manager bot 在线，并在 `/status` 和 `/backend` 里显示错误。这样你还能从 Telegram 里重试或切到另一个后端。

Codex 和 Claude 在 `state.json` 里分别保存自己的项目、线程、模型、回复绑定和标签状态。细节见 [`docs/backend-switching.md`](docs/backend-switching.md)。

## Telegram 命令

常用命令：

| 命令 | 作用 |
| --- | --- |
| `/start` | 绑定当前 chat，并显示项目选择。 |
| `/backend` | 查看或切换 Codex/Claude 后端。 |
| `/project` | 选择项目。默认显示最近 5 个，再发一次或用 `/project all` 显示全部。 |
| `/thread` | 选择当前项目下的线程。默认显示最近 5 个，再发一次或用 `/thread all` 显示全部。 |
| `/new` | 在当前项目创建新线程。 |
| `/status` | 查看后端、项目、线程、活跃 turn、队列、审批和 goal 状态。 |
| `/queue <text>` | 把消息排队，而不是 steer 当前活跃 turn。 |
| `/interrupt` | 中断当前后端 turn，并拒绝该线程的待审批请求。 |
| `/stop` | 停止 Bridge 服务。 |
| `/help` | 显示帮助。 |

线程和历史命令：

| 命令 | 作用 |
| --- | --- |
| `/threadinfo` | 查看当前线程详情。`/threadinfo full` 会包含 turns。 |
| `/rename <name>` | 重命名当前线程。 |
| `/archive` | 归档当前线程。在项目列表里，`/archive 1` 会归档项目 1 下所有未归档线程；在线程列表里，`/archive 1 2` 会归档对应线程。 |
| `/unarchive <threadId>` | 恢复归档线程，并尽量选中它。 |
| `/rollback 1` | 回滚 Codex 线程历史，不会还原工作区文件。 |
| `/compact` | 启动线程 compact。 |
| `/fork [name]` | fork 当前线程，可选重命名，并默认选中新 fork。 |
| `/summary` | 读取 app-server 的 conversation summary。 |
| `/history` | 显示最近 5 个 turns。可用 `/history 10` 或 `/history all`。 |

Codex 和 agent 命令：

| 命令 | 作用 |
| --- | --- |
| `/goal` | 读取当前 goal。 |
| `/goal <objective>` | 设置活跃线程 goal。 |
| `/goal clear` | 清空当前 goal。 |
| `/goal end` | pause goal，中断活跃 turn，然后 clear goal。 |
| `/goal status paused` | 修改 goal 状态。支持 `active`、`paused`、`blocked`、`usageLimited`、`budgetLimited`、`complete`。 |
| `/goal budget 100000 [objective]` | 设置 token budget，可选同时设置目标。 |
| `/approvals` | 查看待处理的 app-server 审批请求。 |
| `/approvals deny 1 2` | 拒绝指定审批。 |
| `/approvals deny all` | 拒绝所有待审批请求。 |
| `/limits` | 查看账号 rate limits。 |
| `/mcp` | 查看 MCP server 状态。`/mcp full` 查看完整信息。 |
| `/review` | 对未提交改动启动 inline review。 |
| `/diff` | 查看相对 remote 的 git diff。 |
| `/config` | 查看后端配置。`/config full` 会包含 config layers。 |
| `/skills` | 查看可用 skills。`/skills reload` 跳过缓存。 |
| `/hooks` | 查看配置的 hooks。 |
| `/apps` | 查看 apps。`/apps refresh` 跳过 app 缓存。 |
| `/plugins` | 查看 plugins。`/plugins <name>` 读取某个 plugin。 |

媒体、模型和认证命令：

| 命令 | 作用 |
| --- | --- |
| `/images` | 列出最近一轮的图片。用 `/images 1` 或 `/images all` 发送到 Telegram。 |
| `/files` | 列出最近一轮的文件。用 `/files 1` 或 `/files all` 发送到 Telegram。 |
| `/model` | 用 Telegram 按钮选择模型和 reasoning effort。 |
| `/fast` | 查看或设置 fast service tier。支持 `/fast on`、`/fast off`、`/fast status`。 |
| `/switch` | 列出或切换准备好的 Codex `auth.json` 账号。只适用于 Codex 后端。 |
| `/doctor` | 运行 Bridge 诊断。 |

Slash 命令只有出现在 Telegram 消息或 caption 开头时才会生效。比如 `please /interrupt this` 会作为普通输入发给当前后端。

## 消息、文件和审批

如果后端支持 same-turn steering，普通 Telegram 消息会 steer 当前活跃 turn。想让消息等当前 turn 结束后再作为新 turn 运行，用 `/queue <message>`。

Telegram 图片会以 `localImage` 发送给 Codex。对于 Claude Code，Bridge 会把图片路径写进 prompt。其他上传文件会下载到 `downloads/`，并以本地文件 mention 加路径说明的形式传给后端。

Codex app-server 请求审批命令、文件改动或权限时，Bridge 会发送 Telegram inline 按钮：

```text
Allow once | Allow in session
Deny
```

待审批请求 10 分钟后过期，并被安全拒绝。如果还没有绑定 Telegram chat，审批请求也会被安全拒绝，避免 turn 一直挂住。

审批消息发送到 Telegram 前会遮蔽常见 token、bearer、password、secret 和 API key。审批决定会以 metadata-only JSON lines 写入 `approval_audit.log`，不会写入命令正文或权限 payload。

## HTTP 控制 API

CC Bridge 会启动本地 HTTP 控制服务，供脚本和桌面集成使用。默认监听 `http://127.0.0.1:8765`，不会主动往 Telegram 发消息。

常用 endpoints：

```text
GET  /health
GET  /help
GET  /commands
GET  /status
GET  /doctor
GET  /backend
POST /backend      {"backend": "codex"} or {"backend": "claude"}
GET  /projects
GET  /threads?cwd=C:\path\to\some-project
POST /project      {"index": 1} or {"cwd": "..."}
POST /thread       {"index": 1} or {"threadId": "...", "cwd": "..."}
POST /new          {"cwd": "..."} or {}
GET  /threadinfo?includeTurns=0
POST /rename       {"name": "new title"}
POST /archive      {}
POST /unarchive    {"threadId": "...", "select": true}
POST /rollback     {"numTurns": 1}
POST /compact      {}
GET  /goal
POST /goal         {"objective": "...", "status": "active", "tokenBudget": 100000}
POST /goal/clear   {}
GET  /summary
GET  /models
POST /model        {"model": "gpt-5.4", "effort": "medium"}
GET  /fast
POST /fast         {"mode": "on"} or {"mode": "off"}
POST /message      {"text": "hello", "timeoutSeconds": 300}
POST /queue        {"text": "run after the active turn", "timeoutSeconds": 300}
POST /interrupt    {}
GET  /limits
GET  /mcp?detail=toolsAndAuthOnly&limit=50
POST /review       {"target": {"type": "uncommittedChanges"}, "delivery": "inline"}
GET  /diff
GET  /config?includeLayers=1
GET  /skills?forceReload=0
GET  /hooks
POST /fork         {"name": "experiment", "select": true}
GET  /apps?forceRefetch=0&limit=50
GET  /plugins
GET  /plugins?pluginName=github
GET  /auth/accounts
POST /auth/switch  {"account": "user@example.com"} or {"index": 1}
POST /stop         {}
GET  /history?limit=5
GET  /history?all=1
```

`POST /message` 会同步运行一个 HTTP turn，并返回最终文本。如果已有活跃 turn，它会尝试 `turn/steer`，除非传入 `"steer": false`。`POST /queue` 会等待当前 turn lock，然后启动新的 HTTP turn。

如果设置了 `HTTP_CONTROL_TOKEN`，请求需要带上 `Authorization: Bearer <token>` 或 `X-Codex-Bridge-Token: <token>`。

`/backend` 和 `POST /backend` 只切换本地 Bridge 后端，不会修改 Telegram Bot token、允许 chat id 或 Codex 账号。

## Codex 账号切换

`/switch` 会扫描准备好的 Codex auth 备份：

```text
D:\Backups\codex-auth\<account>\auth.json
```

使用 `/switch` 列出账号，再发送数字选择；也可以直接用 `/switch <account>`。

只有当前 Codex 账号和目标账号都在备份清单中时，Bridge 才会执行切换。如果目标账号已经是当前账号，Bridge 会提示 unchanged，不会重启 app-server。

切换时，Bridge 会停止 app-server，把当前全局 `%USERPROFILE%\.codex\auth.json` 备份到当前账号对应目录，再复制目标账号的 auth 文件到全局位置，然后重启 app-server。如果 Codex 正在回复，切换会被拒绝；先用 `/interrupt`。

## 项目结构

```text
cc_bridge/
  __main__.py             CLI 入口：python -m cc_bridge
  main.py                 CLI 参数和启动检查
  config.py               config.local.py 加载和常量
  state.py                state.json 读写
  request_parsing.py      HTTP 和命令 payload 解析
  tray.py                 tray 图标应用

  appserver/              Codex JSON-RPC 和 Claude CLI 适配
  assets/                 tray 图标
  core/                   bridge service、turns、threads、models、diagnostics
  formatting/             Telegram 和 HTTP 文本格式化
  http/                   本地 HTTP 控制服务
  platform/               Windows、macOS、Linux 进程处理
  telegram/               Bot API client、handlers、commands、Markdown

docs/                     设计说明和兼容性文档
scripts/                  辅助脚本
tests/                    unittest 测试
```

## 相关文档

- [`docs/backend-switching.md`](docs/backend-switching.md)：后端切换和状态隔离。
- [`docs/telegram-routing.md`](docs/telegram-routing.md)：Telegram 路由、启动上下文、回复路由、多线程执行和线程级模型配置。
- [`docs/app-server-api.md`](docs/app-server-api.md)：Codex app-server JSON-RPC 兼容性记录。

## 安全说明

- 不要提交 `config.local.py`，里面有 Telegram Bot token 和允许访问的 chat id。
- 不要提交 `state.json`、`downloads/`、`bridge.log`、`approval_audit.log`、`cc_bridge.lock` 和认证文件。
- HTTP 服务建议保持 loopback 绑定。如果绑定到非 loopback 地址，必须设置 `HTTP_CONTROL_TOKEN`。
- 审批审计日志只记录 metadata，不保存命令正文或权限 payload。

## 开发

发布或提交前建议运行：

```powershell
python -B -m unittest discover -s tests
python -m cc_bridge --doctor
```

只想检查本地配置和后端启动时，用：

```powershell
python -m cc_bridge --check
```
