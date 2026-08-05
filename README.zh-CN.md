# hermes-codex-bridge

> [English](README.md) | **简体中文**

Codex 与 Hermes Agent 之间的**双向本地协作**桥，基于
[A2A 协议](https://a2a-protocol.org)（JSON-RPC over HTTP，仅回环地址）。

一个仓库包含两个组件：

| 组件 | 文件 | 作用 |
|---|---|---|
| **Codex → Hermes** | `hermes_mcp_server.py` | 纯标准库的 stdio **MCP 服务器**，暴露一个 `call_hermes` 工具：向本地 Hermes 发送任务（A2A `message/send`）、轮询 `tasks/get`、把 Hermes 的最终回复作为 MCP 文本结果返回。 |
| **Hermes → Codex** | `codex_a2a_bridge.py` | 本地回环 **HTTP 桥**，向 Hermes 暴露 A2A `message/send` / `tasks/*`；每个任务启动原生 Codex CLI（`codex exec`）子进程，带并发上限、token 鉴权、心跳与崩溃恢复。 |

```
┌────────────┐   stdio MCP    ┌──────────────────┐   A2A JSON-RPC   ┌──────────────────┐
│  Codex CLI │ ◄────────────► │ hermes_mcp_server │ ◄──────────────► │  Hermes Agent    │
│ (客户端)    │  tools/call    │  (本仓库)         │   message/send   │ (A2A gateway)    │
└────────────┘                └──────────────────┘   tasks/get      └──────────────────┘
                                                                          ▲
┌────────────┐                ┌──────────────────┐   A2A JSON-RPC        │
│  Hermes    │ ◄────────────► │ codex_a2a_bridge │ ◄─────────────────────┘
│ (A2A peer) │   message/send │  (本仓库)         │   codex exec (子进程)
└────────────┘                └──────────────────┘
```

## 特性

**MCP 服务器（`hermes_mcp_server.py`）**
- 纯 Python 标准库，零第三方依赖。
- 走 stdio 的 MCP 协议，**双 framing** 兼容：传统 `Content-Length` 头与换行分隔 JSON（响应跟随请求的 framing）。
- JSON-RPC 批处理支持；实现 `initialize` / `tools/list` / `tools/call` / `ping`。
- Profile 路由：一个工具 → 多个 Hermes 实例（每个 profile 映射到独立 A2A 端点）。
- 进行中任务跟踪：超时后尽力调用 `tasks/cancel`；EOF / SIGINT / SIGTERM 时取消全部已记录任务；下次启动时对残留记录做对账（WORKING 的取消、终态的清除、不可达的保留）。对账失败绝不阻塞启动。
- 输入限制（消息长度、`context_id` 字符集/长度）、响应体积上限、日志脱敏。

**桥（`codex_a2a_bridge.py`）**
- 默认仅回环（`127.0.0.1`；未显式传 `--host` 时拒绝非回环地址）。
- 可选共享 Bearer token（`--token`、`--token-file` 或 `A2A_BRIDGE_TOKEN`）。
- 并发上限（`--max-concurrent`）、基于心跳的崩溃检测、满队列时拒绝任务、`/health` 健康检查。
- 处理终态任务（completed / failed / canceled / rejected），把结果流式回传给 Hermes。

## 环境要求

- Python 3.10+（仅标准库）
- [Codex CLI](https://github.com/openai/codex) 已安装并登录（仅桥需要）
- Hermes Agent 已启用 A2A 入站平台（gateway 的 `platforms.a2a` 监听 127.0.0.1）

## 安装

1. **Hermes 侧** — 在 `config.yaml` 里启用 A2A 入站平台，让 Hermes gateway 监听 `127.0.0.1:9900`（每个要触达的 profile/实例一个端口）：

   ```yaml
   # Hermes config.yaml — 入站平台（别人可以调用这个 Hermes）
   gateway:
     platforms:
       a2a:
         enabled: true
         extra:
           port: 9900        # 每个 profile 一个端口，第二个实例用 9901
   ```

   然后重启 gateway（`hermes gateway restart`）。确认：
   `curl -s http://127.0.0.1:9900/.well-known/agent-card.json`

2. **MCP 服务器** — 在 Codex 配置里把 `hermes_mcp_server.py` 注册为 MCP 服务器：

   ```toml
   # Codex config.toml
   [mcp_servers.hermes]
   command = "C:\\Path\\To\\Python311\\python.exe"   # 显式写解释器，不要直接写 .py 文件
   args = ["C:\\path\\to\\hermes-codex-bridge\\hermes_mcp_server.py"]
   startup_timeout_sec = 15
   tool_timeout_sec = 300          # Codex 等待 call_hermes 的最大秒数
   enabled = true
   required = false
   enabled_tools = ["call_hermes"]
   ```

   两个实战踩过的坑：
   - **`command` 必须写 Python 解释器**，脚本放 `args`——`command` 直接指向 `.py` 文件会导致初始化失败。
   - 新版 Codex（RMCP）发送换行分隔 JSON 帧；本服务器**两种 framing 都收**（含传统 `Content-Length`），并按请求的 framing 回包。

   之后 Codex 就多了一个 `call_hermes` 工具。独立的协议走查示例见 `tests/mcp_smoke_test.py`。

3. **桥** — 复制 `scripts/start_bridge.example.ps1`，改好路径后运行（或直接 `python codex_a2a_bridge.py --help`）。桥需要原生 `codex.exe` 和一个工作目录。然后在 Hermes 侧把它注册为出站 peer：

   ```yaml
   # Hermes config.yaml — 出站 peer（这个 Hermes 调 Codex）
   a2a_agents:
     codex:
       url: "http://127.0.0.1:9998"
       timeout: 1800
       # 如果桥启用了 token 文件，把内容同步到这里：
       auth: { type: bearer, token: "<bridge.token 内容>" }
   ```

## 验证

装完后的快速端到端检查：

```bash
# Hermes Agent Card 可达（入站）
curl -s http://127.0.0.1:9900/.well-known/agent-card.json

# Hermes 入站接受 JSON-RPC 任务（用文件避免 shell 引号转义）
curl -s -X POST http://127.0.0.1:9900 -H "Content-Type: application/json" -d @a2a_inbound_test.json

# 桥的健康检查
curl -s http://127.0.0.1:9998/health
```

完整协议走查（initialize → tools/list → 真实 `call_hermes`）在 `tests/mcp_smoke_test.py`；profile 路由（含非法 profile）在 `tests/mcp_profile_test.py`。

## 配置（环境变量）

`hermes_mcp_server.py`：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `HERMES_A2A_URL` | `http://127.0.0.1:9900` | 主 A2A 端点 |
| `HERMES_PROFILE_URLS` | `{"default": ...9900, "web-dev": ...9901}` | JSON 字典，覆盖 profile → 端点映射 |
| `HERMES_A2A_TOKEN` | *(空)* | 发给 Hermes A2A 端点的 Bearer token |
| `HERMES_TASK_TIMEOUT` | `300` | 等待任务完成的最长秒数 |
| `HERMES_POLL_INTERVAL` | `1.0` | `tasks/get` 轮询间隔（秒） |
| `HERMES_HTTP_TIMEOUT` | `10.0` | 单次 HTTP 请求超时（秒） |
| `HERMES_STATE_FILE` | `~/.hermes/mcp_inflight.json` | 进行中任务状态文件 |

`codex_a2a_bridge.py`：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `HERMES_A2A_WORKSPACE` | 当前目录 | 传给 Codex 的工作目录（或 `--workspace`） |
| `A2A_BRIDGE_TOKEN` | *(空)* | 共享 Bearer token（或 `--token` / `--token-file`） |

## 安全说明

- 两个服务器**只绑定 127.0.0.1**，这是刻意设计——不要把它们暴露到回环之外。
- `call_hermes` 会执行真实的 agent 工作：MCP 客户端只把该工具授予可信的调用方 agent。工具描述本身也警告不要用它把任务反向委派回调用方 agent（防止 agent 间无限循环）。
- 桥的状态目录存放 Bearer token 与任务记录——务必排除在版本控制之外（见 `.gitignore`）。
- `context_id` 在状态文件中只存 SHA-256 前缀，绝不存原始值。
- Hermes 自身的 A2A 平台还有纵深防御（默认全开）：无 token ⇒ 仅回环绑定；出站文本对凭据形状脱敏（`sk-`、`ghp_` 等）；入站文本过滤并标记为不可信 peer 输入；按身份限流 + ping-pong 轮次上限（默认 5）阻断 agent 互调死循环；每次交换记入 `~/.hermes/a2a_audit.jsonl`。

## 已知限制与排查

- **上游返回空回复** — `default` profile 偶尔完成任务但回复为空；服务器如实报告 `empty_reply` 并带 `task_id`，不编造内容。这是上游行为，不是桥的 bug。重试，或把任务路由到回复稳定的 profile。
- **`context_id` 延续受 gateway 生命周期约束** — 会话连续性依赖 Hermes gateway 保留对话；gateway 重启或手动清会话即中断。之后再复用同一 `context_id` 会开启新对话。
- **`--last` 与桥会话冲突** — 桥正在使用某会话时，不要另跑 `codex exec --last`，两者会抢同一线程。
- **resume 退化** — 若 Codex 已清理某会话，桥的 `codex exec resume` 会持续失败；桥会回退开新线程（标记 `resumed_from_new`），但尚无完整自愈。
- **模型不匹配** — 桥默认不传 `--model`，继承你的 Codex 配置；在桥里硬编码模型会在切换供应商时导致不匹配。
- **Windows 进程终止语义** — Windows 上经 TerminateProcess 发送的 SIGTERM 不会触发 Python 处理器；状态文件 + 下次启动对账是兜底恢复。

## 测试

```bash
# 完整单元套件（44 项，无需 Hermes）
python -m unittest discover -s tests -p "test_*.py" -v

# MCP 协议冒烟测试（initialize → tools/list → call_hermes），需要本机
# Hermes A2A gateway 在默认端口运行
python tests/mcp_smoke_test.py

# Profile 路由 + 非法 profile 测试
python tests/mcp_profile_test.py

# 桥单元测试（无需 Hermes，使用假桥）
python tests/test_codex_a2a_bridge.py
```

## License

MIT
