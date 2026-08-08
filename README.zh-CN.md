# hermes-codex-bridge

> [English](README.md) | **简体中文**

Codex 与 Hermes Agent 之间的**双向本地协作**桥，基于
[A2A 协议](https://a2a-protocol.org)（JSON-RPC over HTTP，仅回环地址）。

> **核心卖点：让每个 agent 拥有使用另一个 agent 的能力。**
> Hermes 干活时可以直接调用 Codex（像你指挥一个助手），Codex 也能反过来调用
> Hermes——你不再是人肉传送带，不用在两个工具之间来回复制粘贴上下文。

## 这是什么？（大白话版）

**你机器上的两个 AI 智能体，互相串门。**

- **Codex**（写代码的）和 **Hermes**（你的助手）平时各干各的。这个项目给它们搭了座双向桥：
  - Codex 可以请 Hermes 帮忙（查知识库、抓网页、跑个本地检查），拿到最终答复。
  - Hermes 可以请 Codex 写代码或审代码，拿到成品结果。
- **纯本地**：一切都在你机器上跑（回环地址），数据不经过任何云服务器。
- 自带**实时监控面板**（`/ui`）：双向每一条调用都看得见——按对话分组、带状态、时间和完整记录。就像**一张能看到通话内容的电话账单**。
- 相关调用自动归入**同一对话**（工作线），后续任务延续上下文，不用每次都从头说起。

> 一句话：**两个智能体的总控室。** 想让 Codex 和 Hermes 协作干活、又不想来回复制粘贴上下文的时候，用它就对了。

### 能做什么、解决什么问题

| 目标 | 这个项目怎么帮你 |
|---|---|
| **在智能体之间交接任务** | Codex 请 Hermes 调研/抓取/核对，拿到干净答案；Hermes 请 Codex 写代码/审代码，拿到成品结果——全程不用手动复制粘贴。 |
| **保持对话连贯** | 相关调用自动归入同一工作线（`ctx-<name>#01`、`#02`...），后续任务延续上下文；独立复审单独开线程（`-review`），避免被原上下文带偏。 |
| **所有事情看得见** | 实时面板（`/ui`）展示双向每一条调用：状态、时间、谁说了什么、完整记录——带统计、筛选和搜索。 |
| **用完就清理** | 面板上直接删单条或整组对话；过期会话自动隐藏；连通性测试噪音单独归组，一键清理。 |
| **本地安全** | 仅回环、token 鉴权、崩溃恢复 + 心跳监控——数据不出你的机器。 |

一个仓库包含两个组件：

| 组件 | 文件 | 作用 |
|---|---|---|
| **Codex → Hermes** | `hermes_mcp_server.py` | 纯标准库的 stdio **MCP 服务器**，暴露一个 `call_hermes` 工具：向本地 Hermes 发送任务（A2A `message/send`）、轮询 `tasks/get`、把 Hermes 的最终回复作为 MCP 文本结果返回。 |
| **Hermes → Codex** | `codex_a2a_bridge.py` | 本地回环 **HTTP 桥**，向 Hermes 暴露 A2A `message/send` / `tasks/*`；每个任务启动原生 Codex CLI（`codex exec`）子进程，带并发上限、token 鉴权、心跳与崩溃恢复。 |
| **标准 A2A 接入**（可选）| `a2a_sidecar.py` | 可选的 **A2A 1.0 网关**（`:10000`），面向第三方标准 agent：JSON-RPC/gRPC 双传输、Bearer 鉴权、租户隔离、推送通知、持久化、AgentCard 签名和 OpenTelemetry 追踪——底层复用同一个桥。 |

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
       ▲
       │ A2A 1.0（可选）
┌──────────────────┐
│  a2a_sidecar     │  ← 第三方标准 A2A agent
│  (:10000, SDK)   │
└──────────────────┘
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

**实时监控页（`/ui`）**
- 实时仪表盘：按对话（context_id）分组、方向芯片（inbound/outbound）、状态徽章、代次标记、测试噪音自动归组。
- 统计卡片、状态分布堆叠条、方向环图（纯 CSS）、最近活动流。
- 按角色（用户/助手/工具/系统）的时间轴对话，本地时间显示。
- 状态过滤 + 关键词搜索；单条/整组删除（Bearer token 保护的 `<dialog>` 确认框）。
- 孤儿过滤：Codex 会话文件已不存在的任务自动隐藏。

**A2A SDK sidecar（`a2a_sidecar.py`，可选）** —— 面向第三方 agent 的标准 A2A 1.0 接入
- 标准 A2A 1.0 协议：`SendMessage` / `GetTask` / `ListTasks` / `CancelTask` + AgentCard 发现（`/.well-known/agent-card.json`）。
- **双传输**：HTTP JSON-RPC（`:10000`）和 gRPC（`--grpc-port`，带 Bearer 鉴权拦截器）。
- **安全**：默认只绑 loopback；`--require-token` 强制所有端点 Bearer 鉴权（含 gRPC）；非 loopback 绑定必须鉴权且禁止复用桥管理 token。
- **租户隔离**（`--tenant-mode`）：无 tenant 请求拒绝；跨租户取消拒绝。
- **持久化**（`--db`）：SQLite 任务存储，重启不丢。
- **推送通知**（`--push`）：任务状态变化主动推给客户端配置的 webhook。
- **AgentCard 签名**（`--sign-key`）：JWT 签名卡片，客户端可验证真实性。
- **OpenTelemetry 追踪**（`--tracing`）：SDK span 输出到控制台（无需外部 collector）。
- **SDK 更新检查**（`--check-update`）：查 PyPI 最新 `a2a-sdk`。
- 进程隔离：sidecar 崩溃不影响桥 `:9998`；零依赖降级（未装 `a2a-sdk` 时明确报错，桥照常工作）。

**反向链路上报（Codex → Hermes 可见性）**
- 桥的 `POST /inbound/events`（独立 `--inbound-token`）：MCP server 上报 started/accepted/state/finished 事件，反向调用出现在监控页。
- MCP 侧 durable outbox（JSONL + ack + 指数退避）：崩溃不丢事件；按 operation_id 幂等；tombstone 防止已删任务复活。

**会话管理**
- 工作线注册表：调用方传逻辑名（如 `demo-project`），桥自动解析代次（`ctx-<name>#01`、`#02`...）并 resume 同一 Codex 会话。
- 健康检查（估算 token / 消息数 / 文件大小 / 空闲天数）带 warning 标记；可选自动轮换（`WS_AUTO_ROTATE`）；归档与清理。

**提示词规范（双向）**
- 推荐结构化 message 模板：【目标】【上下文与输入】【边界与授权】【交付与验收】——涉及工具调用/多步骤/改文件/有上下文时默认必须；一次性小任务可用紧凑表述（最低不能是 `hi`）。示例见下。

**消息示例**

*完整任务（默认）：*
```text
请帮我完成以下任务：
【目标】检查 codex_a2a_bridge.py 的 delete_task，找出共享 context 误删会话文件的漏洞
【上下文与输入】文件 codex_a2a_bridge.py，删除逻辑在 delete_task 方法
【边界与授权】只读分析，不修改任何文件；不访问外网
【交付与验收】给出漏洞点（带行号）+ 修复建议，300 字内
```

*紧凑任务（仅限一次性小任务）：*
```text
请直接回答：解释 A2A 的 tasks/get 轮询为什么拿不到中间思考链。只读，3 点以内。
```

*续接已有对话（复用同一 context_id）：*
```text
请帮我完成以下任务：
【已确认】上一轮已确认：白屏根因是 JS 转义，已修复并验证
【本轮目标】现在检查监控页切换对话不更新的问题
【上下文与输入】同上，codex_a2a_bridge.py
【边界与授权】只读分析，不改文件
【交付与验收】根因 + 行号 + 修复建议
```

## 环境要求

- Python 3.10+（仅标准库）
- [Codex CLI](https://github.com/openai/codex) 已安装并登录（仅桥需要）
- Hermes Agent 已启用 A2A 入站平台（gateway 的 `platforms.a2a` 监听 127.0.0.1）

## 平台支持

| 平台 | 状态 |
|------|------|
| **Windows** | ✅ 一等公民（install.ps1、start_bridge.ps1、PowerShell 脚本）|
| **Linux / macOS** | ⚠️ 尽力支持——提供 bash 启动脚本（`scripts/start_bridge.example.sh`、`start_a2a_sidecar.sh`），桥本身纯 stdlib 天然跨平台 |

> **诚实声明**：bash 脚本是从 PowerShell 移植的，但**尚未在真实 macOS/Linux 上实测**（维护者环境是 Windows）。作为起点提供——欢迎修正和 PR。

## 一键安装（Windows）

```powershell
git clone https://github.com/Drunklvu/hermes-codex-bridge.git
cd hermes-codex-bridge
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

安装器自动探测 Python / Codex / Hermes，注册 MCP server（`~/.codex/config.toml`）、
注册 Hermes 的 codex peer（capabilities 用 Python+PyYAML 写成真正的 YAML 列表）、
创建开机自启、从示例生成真实 `scripts/start_bridge.ps1`（占位符路径和端口自动填充）、
启动桥。

**幂等**：对已配置好的环境重复运行会跳过未变化的配置。卸载用 `-Uninstall`
（停止桥、移除 MCP 注册和自启）；只探测用 `-DryRun`（不写任何东西）。

| 参数 | 作用 |
|------|------|
| `-Python <path>` | MCP server 用的 Python 解释器（默认自动探测 `python.exe`；桥本身用 `pythonw.exe`） |
| `-Codex <path>` | Codex 可执行文件（默认自动探测） |
| `-HermesCli <path>` | Hermes CLI（默认自动探测；沙箱测试可指向假 CLI） |
| `-BridgeDir <path>` | 桥脚本所在目录（默认仓库根） |
| `-Workspace <path>` | Codex 工作目录（默认桥目录的上级） |
| `-Port <int>` | 桥端口（默认 9998） |

> ⚠️ 生成的 `scripts/start_bridge.ps1` 含你的本地绝对路径——是机器本地文件，不要提交。

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
# 完整单元套件（128 项，无需 Hermes）
python -m unittest discover -s tests -p "test_*.py" -v

# MCP 协议冒烟测试（initialize → tools/list → call_hermes），需要本机
# Hermes A2A gateway 在默认端口运行
python tests/mcp_smoke_test.py

# Profile 路由 + 非法 profile 测试
python tests/mcp_profile_test.py

# 桥单元测试（无需 Hermes，使用假桥）
python tests/test_codex_a2a_bridge.py
```

## A2A SDK Sidecar（可选组件，标准 A2A 接入层）

现有桥（:9998）是**私有协议 + 本地 loopback**，只服务 Codex。若未来需要**第三方标准 A2A agent** 接入（远程部署、开放生态），提供可选 sidecar：

```
标准 A2A 客户端 → sidecar (:10000, A2A 1.0) → 桥 (:9998) → Codex
```

### 架构原则（不破坏现有协作）

- sidecar 是**独立进程**，默认不启动；现有桥 :9998 照常工作
- 桥仍是**唯一执行者和状态权威**（Codex 进程/监控页/私有协议全不动）
- sidecar 只做"标准 A2A 传输翻译"，经 `internal/*` 端点调桥
- SDK 任务与现有任务**共享桥状态源**（监控页可见）

### 安装（可选）

```bash
pip install "hermes-codex-bridge[a2a]"   # 或：pip install a2a-sdk[http-server,fastapi]
```

### 启动

```powershell
# 本地最小（HTTP :10000，内存态）
powershell -File start_a2a_sidecar.ps1

# 全特性（持久化 + gRPC + 推送）
powershell -File start_a2a_sidecar.ps1 -Db sidecar_tasks.db -GrpcPort 50051 -Push

# 远程部署（Bearer 鉴权 + 独立 token）
powershell -File start_a2a_sidecar.ps1 -RequireToken -SidecarToken <你的token>
```

参数速查：

| 参数 | 默认 | 说明 |
|------|------|------|
| `-Port` | 10000 | HTTP 监听端口 |
| `-Db` | 空 | SQLite 文件路径（持久化；空=内存态，重启丢失）|
| `-GrpcPort` | 0 | gRPC 监听端口（0=不启用；建议 50051）|
| `-Push` | 关 | 启用推送通知（任务状态主动推给客户端 webhook）|
| `-RequireToken` | 关 | 远程部署加固（所有端点需 Bearer token）|
| `-SidecarToken` | 空 | sidecar 自身 token（RequireToken 时；空=复用桥 token）|
| `-Reasoning` | 关 | 向客户端暴露 Codex 推理事件（默认关：保护模型内部推理隐私）|
| `-BridgeUrl` | :9998 | 内部桥地址 |

> 安全：绑定非 loopback 地址（`-Host 0.0.0.0`）时**强制**需要 `-RequireToken`，
> 且禁止复用桥管理 token（需 `-SidecarToken` 独立指定）。

### 验证

```bash
curl http://127.0.0.1:10000/health          # {"ok":true,"service":"a2a-sidecar"}
curl http://127.0.0.1:10000/.well-known/agent-card.json   # 标准 AgentCard
```

### 安全

- 默认只绑 `127.0.0.1`；远程部署必须 `--require-token`
- sidecar 崩溃/关闭**不影响**桥 :9998（进程隔离）
- 私有路由（/inbound/events、管理端点）**永不暴露**给 sidecar

### 文件

| 文件 | 职责 |
|------|------|
| `a2a_sidecar.py` | sidecar 主程序（AgentExecutor + FastAPI + 鉴权）|
| `http_task_service.py` | HttpTaskService（TaskService 的 HTTP 实现，调桥 internal/*）|
| `task_service.py` | TaskService 窄接口（Protocol + DTO + 适配器）|
| `a2a_sdk_contract_check.py` | SDK 客户端契约测试（集成，需 sidecar + a2a-sdk 环境）|
| `start_a2a_sidecar.ps1` | 启动脚本 |

## License

MIT
