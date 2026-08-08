"""A2A SDK sidecar——标准 A2A 1.0 接入层（阶段 2）。

架构（Codex 共创方案）：
- 独立进程，默认不启动（`python a2a_sidecar.py` 显式运行）
- 只做「标准 A2A 传输」：把第三方 client 的请求翻译成内部 TaskService 调用
- 不碰 Codex CLI / RMCP / 监控页——现有桥（:9998）仍是唯一执行者
- 依赖 a2a-sdk（可选 extra）：`pip install hermes-codex-bridge[a2a]`

端口规划：
- :9998  现有桥（私有协议，loopback，不动）
- :10000 本 sidecar（标准 A2A，默认 loopback）

安全（阶段 5 完整加固，这里先打底）：
- 默认只绑 127.0.0.1
- sidecar 缺失/崩溃不影响 :9998（进程隔离）

用法：
    python a2a_sidecar.py --port 10000 --bridge-url http://127.0.0.1:9998
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from typing import Any

# 兜底：确保能导入同目录的 task_service（不依赖调用方 cwd/PYTHONPATH）
_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

# 可选依赖：没有 a2a-sdk 时给出明确提示（不破坏零依赖默认安装）
try:
    import uvicorn
    from fastapi import FastAPI
    from a2a.server.agent_execution.agent_executor import AgentExecutor
    from a2a.server.agent_execution.context import RequestContext
    from a2a.server.events.event_queue import EventQueue
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import (
        add_a2a_routes_to_fastapi,
        create_agent_card_routes,
        create_jsonrpc_routes,
        create_rest_routes,
    )
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from a2a.server.tasks.task_updater import TaskUpdater
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentInterface,
        AgentProvider,
        Message,
        Part,
        Task,
        TaskState,
        TaskStatus,
    )
    A2A_AVAILABLE = True
except ImportError as e:  # pragma: no cover
    A2A_AVAILABLE = False
    _IMPORT_ERR = e
    # 零依赖降级：定义占位基类，保证模块可导入（仅拒绝启动）
    class AgentExecutor:  # type: ignore[no-redef]
        pass
    class RequestContext:  # type: ignore[no-redef]
        pass
    class EventQueue:  # type: ignore[no-redef]
        pass
    class TaskUpdater:  # type: ignore[no-redef]
        async def complete(self): pass
        async def failed(self): pass
        async def cancel(self): pass
        async def reject(self): pass
        async def start_work(self, **kw): pass
        async def add_artifact(self, **kw): pass
        def new_agent_message(self, **kw): return None
    class FastAPI:  # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass
        def get(self, *a, **kw): return lambda f: f
        def middleware(self, *a, **kw): return lambda f: f
    class AgentCard:  # type: ignore[no-redef]
        pass

logger = logging.getLogger("a2a-sidecar")


# ---------------------------------------------------------------------------
# AgentExecutor——桥接内部 TaskService（仅 SDK 可用时定义）
# ---------------------------------------------------------------------------

class BridgeAgentExecutor(AgentExecutor):  # type: ignore[misc]
    """把标准 A2A 请求翻译成内部 TaskService 调用（经 HTTP 或直连）。"""

    def __init__(self, task_service: Any, tenant_mode: bool = False,
                 expose_reasoning: bool = False) -> None:
        self._svc = task_service
        self._running: set[str] = set()
        self._tenant_mode = tenant_mode
        self._expose_reasoning = expose_reasoning
        # tenant 前缀 -> context 命名空间（多租户隔离）
        self._tenant_ctx: dict[str, set[str]] = {}
        # SDK task_id -> bridge task_id（取消/查询必须用桥 ID）
        self._task_to_bridge: dict[str, str] = {}
        # task_id -> tenant（租户隔离：跨租户取消/查询拒绝）
        self._task_to_tenant: dict[str, str] = {}
        self._idem_lock: threading.Lock = threading.Lock()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """执行任务：submit -> 轮询核心事件 -> 推送到 SDK event_queue。"""
        msg = context.message
        task_id = context.task_id
        context_id = context.context_id
        if not msg or not task_id:
            return

        # 0. 租户隔离（tenant_mode 开启时）：记录 tenant 归属（不改 context_id，
        #    避免破坏 SDK TaskManager 内部一致性）
        tenant = getattr(context, "tenant", "") or ""
        if self._tenant_mode:
            if not tenant:
                # 开启租户模式但请求无 tenant：拒绝（防跨租户访问）
                raise ValueError("tenant mode 开启但请求缺少 tenant")
            self._tenant_ctx.setdefault(tenant, set()).add(task_id)
            self._task_to_tenant[task_id] = tenant

        # 1. 提交到核心（幂等键 = task_id，防止重复执行）
        import asyncio as _aio
        from task_service import SubmitRequest
        result = await _aio.to_thread(
            self._svc.submit,
            SubmitRequest(
                prompt=msg.parts[0].text if msg.parts else "",
                context_id=context_id or "a2a-external",
                idempotency_key=task_id,
            ),
        )
        # 桥 ID（task-xxx）与 SDK task_id（UUID）不同——必须用桥 ID 轮询
        bridge_task_id = result.task_id if result else ""
        self._running.add(task_id)
        with self._idem_lock:
            self._task_to_bridge[task_id] = bridge_task_id or task_id

        # 2. 先 enqueue Task（SDK 要求：Task 必须先于 TaskStatusUpdateEvent）
        await event_queue.enqueue_event(Task(
            id=task_id,
            context_id=context_id or "",
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            history=[msg],
        ))
        # 3. 提交到核心，轮询事件直到终态（阶段 3 真实链路）
        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id or "")
        await updater.start_work(
            message=updater.new_agent_message(parts=[Part(text="任务已提交到 Codex 桥，执行中...")])
        )
        import asyncio
        # 4. 轮询核心状态直到终态，同时把核心事件推成 SDK 流式事件
        poll = 0
        last_seq = -1
        last_artifact_text: str | None = None
        while poll < 90:
            await asyncio.sleep(2)
            tv = await asyncio.to_thread(self._svc.get, bridge_task_id)
            if tv is None:
                poll += 1
                continue
            # 增量事件：把新事件推成 artifact（流式响应，assistant 消息 + 思考块）
            evs, latest = await asyncio.to_thread(self._svc.events, bridge_task_id, after=last_seq)
            if latest > last_seq:
                last_seq = latest
                for e in evs:
                    if e.role == "assistant" and e.text:
                        last_artifact_text = e.text
                        await updater.add_artifact(
                            parts=[Part(text=e.text)], name="response",
                            last_chunk=False,
                        )
                    elif e.type == "reasoning" and e.text and e.text != "[Codex 正在思考…]" \
                            and self._expose_reasoning:
                        # 思考块：单独 artifact（仅 --reasoning 启用时推送，
                        # 默认不暴露模型内部推理给外部客户端）
                        await updater.add_artifact(
                            parts=[Part(text="🤔 " + e.text)], name="reasoning",
                            last_chunk=False,
                        )
            state = tv.state
            if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"):
                # 终态：只封口一次（补 last_chunk=True）
                if last_artifact_text is None:
                    evs, _ = await asyncio.to_thread(self._svc.events, bridge_task_id, after=-1)
                    for e in evs:
                        if e.role == "assistant" and e.text:
                            last_artifact_text = e.text
                            await updater.add_artifact(
                                parts=[Part(text=e.text)], name="response", last_chunk=True,
                            )
                            break
                if state == "TASK_STATE_COMPLETED":
                    await updater.complete()
                elif state == "TASK_STATE_FAILED":
                    await updater.failed()
                elif state == "TASK_STATE_REJECTED":
                    await updater.reject()
                else:
                    await updater.cancel()
                return
            poll += 1
        # 超时兜底（#4）：不伪造取消——任务可能仍在桥端运行，
        # SDK 侧标记为失败（真实超时），由桥端心跳/清理机制接管
        await updater.failed()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        # 租户校验：跨租户取消拒绝（task 归属 != 请求 tenant）
        if self._tenant_mode:
            req_tenant = getattr(context, "tenant", "") or ""
            owner = self._task_to_tenant.get(task_id or "", "")
            # 拒绝条件：无 tenant、归属未知（可能非本进程创建）、归属不匹配
            if not req_tenant or owner != req_tenant:
                return  # 无 tenant 或非归属者：静默拒绝
        if task_id in self._running:
            self._running.remove(task_id)
        # 调真实取消（桥 internal/cancel），并同步 SDK 状态
        # 必须用桥 ID（SDK task_id 是 UUID，桥端是 task-xxx）
        import asyncio as _aio
        bridge_id = self._task_to_bridge.get(task_id or "", task_id or "")
        ok = await _aio.to_thread(self._svc.cancel, bridge_id)
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task_id or "",
            context_id=context.context_id or "",
        )
        if ok:
            await updater.cancel()
        # 取消失败（如任务不可取消）则保持原状态


if not A2A_AVAILABLE:  # pragma: no cover
    # SDK 未安装时占位，保证模块可导入（零依赖路径不受影响）
    class _Placeholder:  # type: ignore[no-redef]
        pass

    class BridgeAgentExecutor(_Placeholder):  # type: ignore[no-redef]
        pass


# ---------------------------------------------------------------------------
# Sidecar 组装
# ---------------------------------------------------------------------------

def build_app(task_service: Any, host: str = "127.0.0.1", port: int = 10000,
              db_path: str | None = None, grpc_port: int = 0,
              enable_push: bool = False, tenant_mode: bool = False,
              sign_key: str | None = None, expose_reasoning: bool = False) -> FastAPI:
    """构造 FastAPI app（标准 A2A 路由 + AgentCard + 健康检查）。

    db_path: SQLite 文件路径（持久化 TaskStore）；None = 内存态（重启丢失）
    grpc_port: gRPC 监听端口；>0 时 AgentCard 声明 GRPC interface
    enable_push: 启用推送通知（任务完成主动推给客户端 webhook）
    """
    if not A2A_AVAILABLE:  # pragma: no cover
        raise RuntimeError("a2a-sdk 未安装，sidecar 不可用（pip install hermes-codex-bridge[a2a]）")
    app = FastAPI(title="hermes-codex-bridge A2A Gateway", version="0.2.0")

    executor = BridgeAgentExecutor(task_service, tenant_mode=tenant_mode,
                                   expose_reasoning=expose_reasoning)
    if db_path:
        # 持久化 TaskStore（SQLite 文件，标准库 aiosqlite + SQLAlchemy）
        from sqlalchemy.ext.asyncio import create_async_engine as _create_engine
        from a2a.server.tasks.database_task_store import DatabaseTaskStore
        _engine = _create_engine(f"sqlite+aiosqlite:///{db_path}")
        task_store = DatabaseTaskStore(engine=_engine)
    else:
        task_store = InMemoryTaskStore()  # 内存态（默认）

    agent_card = AgentCard(
        name="Hermes Codex Bridge",
        description="Standard A2A gateway to a local Codex agent (via hermes-codex-bridge)",
        provider=AgentProvider(organization="hermes-codex-bridge", url="https://github.com/Drunklvu/hermes-codex-bridge"),
        version="0.2.0",
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=bool(enable_push),
            extended_agent_card=True,
        ),
        default_input_modes=["text"],
        default_output_modes=["text", "task-status"],
        supported_interfaces=[
            AgentInterface(
                url=f"http://{host}:{port}",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            ),
        ] + ([
            AgentInterface(
                url=f"{host}:{grpc_port}",
                protocol_binding="GRPC",
                protocol_version="1.0",
            )
        ] if grpc_port else []),
    )

    push_sender = None
    _push_store = None
    if enable_push:
        # 推送通知：任务完成/状态变化主动推给客户端配置的 webhook
        import httpx as _httpx
        from a2a.server.tasks.base_push_notification_sender import BasePushNotificationSender
        from a2a.server.tasks.inmemory_push_notification_config_store import (
            InMemoryPushNotificationConfigStore,
        )
        _push_store = InMemoryPushNotificationConfigStore()
        push_sender = BasePushNotificationSender(
            httpx_client=_httpx.AsyncClient(timeout=10),
            config_store=_push_store,
        )

    # 签名验证（--sign-key）：给 AgentCard 加 JWT 签名（客户端可用公钥验证防伪造）
    if sign_key:
        try:
            from a2a.utils import signing as _signing
            from a2a.utils.signing import ProtectedHeader as _ProtectedHeader
            key_data = open(sign_key, "rb").read()
            _signer = _signing.create_agent_card_signer(
                signing_key=key_data,
                protected_header=_ProtectedHeader(alg="RS256", typ="JWT"),
            )
            agent_card = _signer(agent_card)
            logger.info("AgentCard 已签名（%s）", sign_key)
        except Exception as e:
            logger.error("AgentCard 签名失败: %s（忽略，继续未签名启动）", e)

    class _ExtendedHandler(DefaultRequestHandler):
        """覆盖 on_get_extended_agent_card：返回扩展卡片（含运行时状态）。"""

        async def on_get_extended_agent_card(self, params, context):
            from a2a.types import AgentCard as _AC
            from a2a.types import AgentCapabilities as _Cap
            ext = _AC()
            ext.CopyFrom(agent_card)
            ext.capabilities.CopyFrom(_Cap(
                streaming=False,
                push_notifications=bool(enable_push),
                extended_agent_card=True,
            ))
            # 扩展字段：运行时信息（功能列表，AgentSkill 结构）
            from a2a.types import AgentSkill as _Skill
            for sid, label in [
                ("grpc", f"grpc:{'on' if grpc_port else 'off'}"),
                ("push", f"push:{'on' if enable_push else 'off'}"),
                ("tenant", f"tenant-mode:{'on' if tenant_mode else 'off'}"),
                ("persist", f"persistence:{'on' if db_path else 'off'}"),
            ]:
                ext.skills.add(id=sid, name=label, description=label)
            return ext

    handler = _ExtendedHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
        push_config_store=_push_store if enable_push else None,
        push_sender=push_sender,
    )
    app.state.a2a_handler = handler  # 供 gRPC server 复用

    # 标准 A2A 路由（1.1.2：先建 routes 再挂载）
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
        rest_routes=create_rest_routes(handler),
    )

    # 健康检查（sidecar 自己的，区别于 :9998 的桥健康）
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "a2a-sidecar", "bridge": "external"}

    return app


try:  # grpc 可选依赖：无 grpcio 时优雅降级
    import grpc.aio as _grpc_aio
except ImportError:
    _grpc_aio = None


if A2A_AVAILABLE and _grpc_aio is not None:
    class _GrpcAuthInterceptor(_grpc_aio.ServerInterceptor):
        """gRPC Bearer token 拦截器（require_token 时启用）。

        校验每个请求的 authorization metadata，无 token/错 token 拒绝。
        """

        def __init__(self, token: str) -> None:
            self._token = token

        async def intercept_service(self, continuation, handler_call_details):
            metadata = dict(handler_call_details.invocation_metadata or [])
            auth = metadata.get("authorization", "")
            if auth != f"Bearer {self._token}":
                from grpc import StatusCode
                return _grpc_aio.unary_unary_rpc_method_handler(
                    lambda request, context: _abort_unauthorized(context),
                    request_deserializer=None,
                    response_serializer=None,
                )
            return await continuation(handler_call_details)


    def _abort_unauthorized(context):
        from grpc import StatusCode
        context.abort(StatusCode.UNAUTHENTICATED, "missing or invalid token")


def start_grpc_server(handler: Any, port: int, require_token: bool = False,
                      token: str = "") -> None:
    """启动 gRPC server（独立线程，挂载 A2A 全部方法）。

    用法：--grpc-port 50051 时启用。注册 A2AServiceServicer，
    客户端可用 gRPC 传输（性能优于 HTTP 轮询）。
    安全：require_token 时启用 Bearer 拦截器；默认绑 127.0.0.1。
    """
    import asyncio as _asyncio
    import grpc as _grpc
    from a2a.server.request_handlers.grpc_handler import GrpcHandler
    from a2a.types.a2a_pb2_grpc import add_A2AServiceServicer_to_server

    grpc_handler = GrpcHandler(handler)

    async def _serve() -> None:
        # 拦截器仅在 grpcio 可用时定义；require_token 但无拦截器 = 拒绝启动（防裸奔）
        if require_token and token and _grpc_aio is None:
            print("[a2a-sidecar] 错误: --require-token 需要 grpcio 才能启用 gRPC 鉴权", file=sys.stderr)
            return
        interceptors = [_GrpcAuthInterceptor(token)] if require_token and token else None
        server = _grpc_aio.server(interceptors=interceptors)
        add_A2AServiceServicer_to_server(grpc_handler, server)
        # 默认 loopback；远程部署（require_token）时绑所有接口
        bind_addr = f"0.0.0.0:{port}" if require_token else f"127.0.0.1:{port}"
        server.add_insecure_port(bind_addr)
        await server.start()
        print(f"[a2a-sidecar] gRPC server 已启动: {bind_addr}", flush=True)
        await server.wait_for_termination()

    import threading as _threading
    _t = _threading.Thread(target=lambda: _asyncio.run(_serve()), daemon=True)
    _t.start()


def add_auth_middleware(app: FastAPI, token: str) -> None:
    """给 FastAPI app 加 Bearer 鉴权（阶段 5 远程加固）。

    - /health、/agent-card 放行（发现/健康检查）
    - 其余端点需 Authorization: Bearer <token>
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.url.path in ("/health", "/.well-known/agent-card.json"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            return JSONResponse(
                {"error": "unauthorized", "message": "missing or invalid Bearer token"},
                status_code=401,
            )
        return await call_next(request)


def _setup_tracing() -> None:
    """启用 OpenTelemetry 追踪：SDK 内部 span 输出到控制台。

    通过环境变量 OTEL_INSTRUMENTATION_A2A_SDK_ENABLED 启用 SDK 追踪，
    并初始化 console exporter（无外部 collector 也能看 span）。
    """
    import os as _os
    _os.environ["OTEL_INSTRUMENTATION_A2A_SDK_ENABLED"] = "true"
    try:
        from opentelemetry import trace as _trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
        _provider = TracerProvider()
        _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        _trace.set_tracer_provider(_provider)
        logger.info("OpenTelemetry 追踪已启用（span 输出到控制台）")
    except ImportError:
        logger.warning("opentelemetry 未安装，--tracing 无效（pip install opentelemetry-sdk）")


def check_sdk_update() -> int:
    """检查 a2a-sdk 是否有新版本（查 PyPI JSON API）。

    用法：python a2a_sidecar.py --check-update
    不启动服务，只查版本。返回 0=已最新，1=有新版本或检查失败。
    """
    import json as _json
    import urllib.request as _url

    # 本地版本
    try:
        from importlib.metadata import version as _ver
        local = _ver("a2a-sdk")
    except Exception:
        local = "未安装"
    print(f"当前版本: a2a-sdk {local}")

    # PyPI 最新版本（10 秒超时，失败不阻塞）
    try:
        req = _url.Request(
            "https://pypi.org/pypi/a2a-sdk/json",
            headers={"User-Agent": "hermes-codex-bridge/0.2 (update-check)"},
        )
        with _url.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        latest = data.get("info", {}).get("version", "")
        print(f"最新版本: a2a-sdk {latest}")
        if local == "未安装":
            print("建议: pip install 'hermes-codex-bridge[a2a]'")
            return 1
        if latest and local != latest:
            print(f"建议升级: pip install 'a2a-sdk[http-server,fastapi]=={latest}'")
            print("升级后请跑契约测试: a2a_sdk_contract_check.py")
            return 1
        print("已是最新版本 ✅")
        return 0
    except Exception as e:
        print(f"⚠️ 检查失败（网络或 PyPI 不可达）: {e}")
        print("请稍后重试，或访问 https://pypi.org/project/a2a-sdk/ 手动查看")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="A2A SDK sidecar（标准 A2A 接入层）")
    parser.add_argument("--port", type=int, default=10000, help="监听端口（默认 10000）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:9998", help="内部桥地址")
    parser.add_argument("--token", default="", help="桥的 Bearer token（internal 端点鉴权）")
    parser.add_argument("--require-token", action="store_true",
                        help="远程部署加固：所有 A2A 端点需 Bearer token")
    parser.add_argument("--sidecar-token", default="",
                        help="sidecar 自身的鉴权 token（require-token 时生效，默认复用桥 token）")
    parser.add_argument("--check-update", action="store_true",
                        help="检查 a2a-sdk 是否有新版本（查 PyPI，无需启动服务）")
    parser.add_argument("--db", default="", metavar="PATH",
                        help="SQLite 文件路径（TaskStore 持久化；默认内存态，重启丢失）")
    parser.add_argument("--grpc-port", type=int, default=0,
                        help="gRPC 监听端口（默认 0=不启用 gRPC；建议 50051）")
    parser.add_argument("--push", action="store_true",
                        help="启用推送通知（任务状态变化主动推给客户端 webhook）")
    parser.add_argument("--tenant-mode", action="store_true",
                        help="多租户隔离：任务 context 加 tenant 前缀，无 tenant 请求拒绝")
    parser.add_argument("--reasoning", action="store_true",
                        help="启用 reasoning 事件推送（默认关：不暴露模型内部推理给外部客户端）")
    parser.add_argument("--sign-key", default="", metavar="PEM",
                        help="AgentCard 签名私钥（PEM 文件）；客户端可用对应公钥验证防伪造")
    parser.add_argument("--tracing", action="store_true",
                        help="启用 OpenTelemetry 追踪（SDK 内部 span 输出到控制台）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if args.check_update:
        return check_sdk_update()

    if not A2A_AVAILABLE:  # pragma: no cover
        print(
            "a2a-sdk 未安装。sidecar 是可选组件，需要时执行：\n"
            "    pip install 'hermes-codex-bridge[a2a]'",
            file=sys.stderr,
        )
        return 1

    logger.info("A2A sidecar 启动: %s:%d -> bridge %s", args.host, args.port, args.bridge_url)

    # 阶段 3：真实 HTTP TaskService（调桥 :9998 的 internal/* 端点）
    from http_task_service import HttpTaskService
    # token 优先 --token，否则从桥 state 目录自动读（同机部署，探测多个常见位置）
    token = args.token
    if not token:
        import os as _os
        _here = _os.path.dirname(_os.path.abspath(__file__))
        _candidates = [
            _os.path.join(_here, "..", ".codex-a2a"),      # 发布版仓库相邻
            _os.path.join(_here, "..", "tools", ".codex-a2a"),  # 本地开发布局
            _os.path.join(_here, ".codex-a2a"),            # 仓库内
        ]
        for _sd in _candidates:
            _tf = _os.path.join(_sd, "bridge.token")
            if _os.path.exists(_tf):
                token = open(_tf, encoding="utf-8").read().strip()
                logger.info("从 %s 读取桥 token", _tf)
                break
        if not token:
            logger.warning("未找到桥 token（探测 %d 个位置）——internal 调用将 401。"
                           "请用 --token 显式指定桥 Bearer token", len(_candidates))
    service = HttpTaskService(
        bridge_url=args.bridge_url,
        token=token,
    )

    if args.tracing:
        _setup_tracing()

    app = build_app(service, host=args.host, port=args.port,
                    db_path=args.db or None, grpc_port=args.grpc_port,
                    enable_push=args.push, tenant_mode=args.tenant_mode,
                    sign_key=args.sign_key or None,
                    expose_reasoning=args.reasoning)
    if args.grpc_port:
        start_grpc_server(
        app.state.a2a_handler,
        args.grpc_port,
        require_token=bool(args.require_token),
        token=args.sidecar_token or token,
    )
    # 安全强制（#3）：非 loopback 绑定必须鉴权，且禁止复用桥管理 token
    is_loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if not is_loopback and not args.require_token:
        logger.error("绑定非 loopback 地址（%s）必须启用 --require-token", args.host)
        return 1
    if not is_loopback and args.sidecar_token == token and token:
        logger.error("远程部署禁止复用桥管理 token，请用 --sidecar-token 指定独立 token")
        return 1
    if args.require_token:
        auth_token = args.sidecar_token or token
        if not auth_token:
            logger.error("--require-token 需要 --sidecar-token 或桥 token")
            return 1
        add_auth_middleware(app, auth_token)
        logger.info("A2A 端点鉴权已启用（Bearer token）")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
