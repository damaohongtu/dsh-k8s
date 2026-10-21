"""WebSocket 反向代理 — 浏览器 WS ↔ 用户 dsh Pod 的 /api/remote.mux。

dsh API 网关的流式通道(remote.mux)承载所有 Remote 流与 $events 事件,
浏览器以无子协议、带会话 cookie 的升级请求连接。沿用 pi-k8s 的 WS 桥接
形态,凭据注入改为 dsh 会话 cookie,并补上与 HTTP 路径同款的两级自愈:
- 上游握手 401 → 失效缓存会话 → 重读日志 token 重新代换 → 重试一次
- 仍 401 → SandboxAuthMismatch 语义 → 编排层删 Pod 重建后最后试一次
- 任一侧断开 → 双侧关闭;沙盒活跃租约由调用方 retain/release 包裹
"""

from __future__ import annotations

import asyncio
import logging

from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from app.sandbox_manager import SandboxHandle, SandboxManager

logger = logging.getLogger(__name__)


class _WsClosed(Exception):
    """内部信号:浏览器侧断开,桥接结束。"""


class _UpstreamUnauthorized(Exception):
    """上游 WS 握手 401:需要重新代换会话。"""


def _upstream_uri(handle: SandboxHandle, path: str, query: str) -> str:
    uri = f"{handle.base_url.replace('http://', 'ws://')}{path}"
    if query:
        uri = f"{uri}?{query}"
    return uri


async def _pump_browser_to_upstream(ws: WebSocket, upstream) -> None:
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            raise _WsClosed()
        if "text" in msg and msg["text"] is not None:
            await upstream.send(msg["text"])
        elif "bytes" in msg and msg["bytes"] is not None:
            await upstream.send(msg["bytes"])


async def _pump_upstream_to_browser(upstream, ws: WebSocket) -> None:
    async for message in upstream:
        if isinstance(message, str):
            await ws.send_text(message)
        else:
            await ws.send_bytes(message)


async def _bridge_once(
    ws: WebSocket,
    manager: SandboxManager,
    user_id: str,
    handle: SandboxHandle,
    path: str,
) -> None:
    """一次 WS 桥接尝试;上游握手 401 时抛 _UpstreamUnauthorized。"""
    auth = await asyncio.to_thread(manager.get_valid_auth, user_id, handle)
    uri = _upstream_uri(handle, path, ws.url.query)
    try:
        async with connect(
            uri,
            # websockets 客户端默认不发送 Origin —— dsh 对带 Origin 的请求一律
            # 403,集群内桥接恰好绕开;会话 cookie 绑定 PodIP:3080 authority,
            # 与这里的 URL 一致
            additional_headers={"cookie": auth.cookie_header},
            open_timeout=10,
            close_timeout=5,
            max_size=64 * 1024 * 1024,
        ) as upstream:
            await ws.accept()
            tasks = [
                asyncio.create_task(_pump_browser_to_upstream(ws, upstream)),
                asyncio.create_task(_pump_upstream_to_browser(upstream, ws)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                if not task.cancelled():
                    task.result()  # _WsClosed / Disconnect 交给外层吞掉
    except InvalidStatus as e:
        if e.response.status_code == 401:
            manager.invalidate_auth(user_id)
            raise _UpstreamUnauthorized(user_id)
        raise


async def proxy_websocket_with_auth(
    ws: WebSocket,
    manager: SandboxManager,
    user_id: str,
    handle: SandboxHandle,
    path: str,
) -> None:
    """完整 WS 桥接编排(认证代换 + 两级自愈,由 main 的 WS catch-all 调用)。"""
    try:
        try:
            await _bridge_once(ws, manager, user_id, handle, path)
            return
        except _UpstreamUnauthorized:
            pass
        # 401 自愈:cookie 失效(dsh 会话密钥轮换等)→ 重新代换 → 再试一次
        try:
            await _bridge_once(ws, manager, user_id, handle, path)
            return
        except _UpstreamUnauthorized:
            pass
        # 终局自愈:新会话仍被拒 → 沙盒状态异常,删 Pod 重建(数据在 PVC 不丢)
        handle = await asyncio.to_thread(manager.recreate, user_id)
        await _bridge_once(ws, manager, user_id, handle, path)
    except _WsClosed:
        pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WS 桥接异常 user={user_id} path={path}: {e}")
        try:
            await ws.close(code=1011)
        except Exception:
            pass
