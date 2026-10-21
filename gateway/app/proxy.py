"""HTTP 反向代理 — 网关 → 用户 dsh Pod(PodIP:3080)。

沿用 openclaw-k8s / opencode-k8s 的代理层形态,认证注入改为 dsh 会话 cookie:
- 过滤客户端的 cookie / authorization(网关会话与 dsh 会话互不渗透),
  注入网关持有的 dsh cookie;Host 由 httpx 置为 PodIP:3080,与 cookie
  绑定的 authority 一致
- 流式转发:大响应不落内存;请求体 ≤ 32MiB 缓冲以支持 401 自愈重放,
  超限则纯流式(不重试)
- retain / release 包住上游调用,防止在途请求被闲置 GC 回收
- 响应过滤 hop-by-hop 与 www-authenticate / set-cookie(dsh 会话完全由
  网关管理,不向浏览器透传);响应体不改动,逐字节透传

dsh 前端的流式通道(WebSocket /api/remote.mux)在 ws_proxy 中桥接,
本模块只处理 HTTP 请求/响应。
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.sandbox_manager import SandboxAuthMismatch, SandboxHandle, SandboxManager

logger = logging.getLogger(__name__)

# 逐跳头:代理必须摘掉,由两侧连接各自协商
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade", "host",
}

# 不透传给 dsh 的请求头:
# - authorization / cookie:客户端凭据与 dsh 会话互不渗透(cookie 由网关注入)
# - origin / referer / sec-fetch-*:dsh 对带 Origin 的 POST 一律 403(实测连与
#   自身 authority 一致的 Origin 也拒);网关→dsh 是集群内服务间调用,剥掉这些
#   浏览器上下文头即可,CSRF 边界由"唯一入口是网关"承担
_CLIENT_ONLY = {
    "authorization", "cookie", "origin", "referer",
    "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user",
}

# 响应侧不透传:www-authenticate(避免浏览器弹认证框)、set-cookie(dsh 会话归网关管理)
_RESPONSE_BLOCKED = _HOP_BY_HOP | {"www-authenticate", "set-cookie"}

# 超过此大小的请求体不缓冲(无法重放),直接流式转发
_MAX_BUFFERED_BODY = 32 * 1024 * 1024

# 响应体超过此大小时走流式转发(小响应整体读出,延迟更低)
_STREAM_THRESHOLD = 1024 * 1024


def get_proxy_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=60.0, pool=5.0),
        # read=None:dsh agent 任务可能长时间无输出,不设读超时
        follow_redirects=False,
        trust_env=False,  # 不吃 http_proxy 等环境变量,直连 Pod IP
    )


def _build_upstream_headers(request: Request, cookie_header: str) -> dict:
    headers: dict = {}
    for key, value in request.headers.items():
        lk = key.lower()
        if lk in _HOP_BY_HOP or lk in _CLIENT_ONLY:
            continue
        headers[lk] = value
    headers["cookie"] = cookie_header
    return headers


def _filter_response_headers(response: httpx.Response) -> dict:
    # 键统一小写:后续 pop content-encoding 等按小写匹配,不依赖上游大小写
    headers = {}
    for key, value in response.headers.items():
        if key.lower() in _RESPONSE_BLOCKED:
            continue
        headers[key.lower()] = value
    return headers


async def _read_body_for_replay(request: Request) -> Optional[bytes]:
    """读请求体:可缓冲(≤ 32MiB)则返回字节以支持重放,否则 None(流式)。"""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return b""
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) <= _MAX_BUFFERED_BODY:
            return await request.body()
    except ValueError:
        pass
    return None


def _is_streaming_response(response: httpx.Response) -> bool:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        return True
    if "attachment" in response.headers.get("content-disposition", ""):
        return True
    content_length = response.headers.get("content-length")
    try:
        return content_length is None or int(content_length) > _STREAM_THRESHOLD
    except ValueError:
        return True


async def send_upstream(
    client: httpx.AsyncClient,
    request: Request,
    handle: SandboxHandle,
    path: str,
    cookie_header: str,
    body: Optional[bytes],
    manager: SandboxManager,
) -> httpx.Response:
    """向用户 dsh Pod 发一次请求(流式响应,未消费)。retain/release 包裹。"""
    url = f"{handle.base_url}{path}"
    headers = _build_upstream_headers(request, cookie_header)
    manager.retain(handle.user_id)
    try:
        if body is not None:
            req = client.build_request(request.method, url, headers=headers, content=body)
            return await client.send(req, stream=True)
        # 超大请求体:从客户端流式搬运,不支持重放
        async def _relay() -> AsyncIterator[bytes]:
            async for chunk in request.stream():
                yield chunk

        req = client.build_request(request.method, url, headers=headers, content=_relay())
        return await client.send(req, stream=True)
    except BaseException:
        manager.release(handle.user_id)
        raise


async def _to_fastapi_response(
    response: httpx.Response, manager: SandboxManager, user_id: str
) -> StreamingResponse:
    headers = _filter_response_headers(response)
    if _is_streaming_response(response):
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=headers,
            background=BackgroundTask(_close_upstream, response, manager, user_id),
        )
    body = await response.aread()
    await _close_upstream(response, manager, user_id)
    # aread() 返回的是 httpx 解码后的明文,而出侧头里还带着上游的
    # content-encoding / content-length(压缩长度)——不摘会与实际字节
    # 不符,浏览器按 gzip 解明文直接报错
    headers.pop("content-encoding", None)
    headers.pop("content-length", None)
    return StreamingResponse(
        iter([body]),
        status_code=response.status_code,
        headers=headers,
    )


async def _close_upstream(
    response: httpx.Response, manager: SandboxManager, user_id: str
) -> None:
    try:
        await response.aclose()
    finally:
        manager.release(user_id)


async def proxy_http(
    client: httpx.AsyncClient,
    request: Request,
    handle: SandboxHandle,
    path: str,
    cookie_header: str,
    manager: SandboxManager,
) -> StreamingResponse:
    """单次代理转发(不做认证重试;重试逻辑在 proxy_with_auth 编排层)。"""
    body = await _read_body_for_replay(request)
    response = await send_upstream(client, request, handle, path, cookie_header, body, manager)
    return await _to_fastapi_response(response, manager, handle.user_id)


# ------------------------------------------------------------------ #
#  请求编排:ensure → 代换认证 → 转发 → 401 自愈                      #
# ------------------------------------------------------------------ #


def _target_path(request: Request, path: str) -> str:
    if request.url.query:
        return f"{path}?{request.url.query}"
    return path


async def _attempt(
    client: httpx.AsyncClient,
    request: Request,
    manager: SandboxManager,
    user_id: str,
    path: str,
) -> Response:
    handle = await asyncio.to_thread(manager.ensure_running, user_id)
    target = _target_path(request, path)
    body = await _read_body_for_replay(request)
    auth = await asyncio.to_thread(manager.get_valid_auth, user_id, handle)
    response = await send_upstream(
        client, request, handle, target, auth.cookie_header, body, manager
    )
    # 401 自愈:cookie 随 dsh 进程重启失效 → 重读日志 token 重新代换 → 重放一次
    # (body 为 None 的超大流式请求不支持重放,直接把 401 交给浏览器,刷新即愈)
    if response.status_code == 401 and body is not None:
        await _close_upstream(response, manager, user_id)
        manager.invalidate_auth(user_id)
        auth = await asyncio.to_thread(manager.get_valid_auth, user_id, handle)
        response = await send_upstream(
            client, request, handle, target, auth.cookie_header, body, manager
        )
        if response.status_code == 401:
            # 新换的会话仍被拒:沙盒状态异常,升级为重建
            await _close_upstream(response, manager, user_id)
            raise SandboxAuthMismatch(user_id)
    return await _to_fastapi_response(response, manager, user_id)


async def proxy_with_auth(
    client: httpx.AsyncClient,
    request: Request,
    manager: SandboxManager,
    user_id: str,
    path: str,
) -> Response:
    """完整代理编排(含认证代换与两级自愈,由 main 的 catch-all 调用)。"""
    try:
        return await _attempt(client, request, manager, user_id, path)
    except SandboxAuthMismatch:
        # 终局自愈:删 Pod 重建(数据在 PVC 不丢),再试最后一次
        handle = await asyncio.to_thread(manager.recreate, user_id)
        target = _target_path(request, path)
        body = await _read_body_for_replay(request)
        auth = await asyncio.to_thread(manager.get_valid_auth, user_id, handle)
        response = await send_upstream(
            client, request, handle, target, auth.cookie_header, body, manager
        )
        return await _to_fastapi_response(response, manager, user_id)
