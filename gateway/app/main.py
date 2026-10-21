"""dsh-gateway — DeepSeek Harness 的 K8s 多用户网关。

架构沿用 openclaw-k8s / opencode-k8s:
    浏览器 ──CAS 单点登录──▶ 网关(本服务,单副本)
                              ├─ SQLite 注册表(网关 PVC)
                              ├─ K8s API:按需创建用户 dsh Pod(PodIP 寻址)
                              ├─ 认证代换:dsh 启动 token → 会话 cookie
                              ├─ HTTP 反代 → PodIP:3080
                              └─ WS 桥接 → PodIP:3080(/api/remote.mux 流式通道)
    闲置 TTL 回收:只删 Pod,PVC(用户数据)永远保留

与 opencode-k8s 的差异:opencode 注入 per-user password 头,dsh 原生
认证是启动 token 换签名 cookie —— 网关代浏览器完成交换并注入 cookie
(见 sandbox_manager / proxy / ws_proxy)。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Union

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from app import auth
from app.config import get_settings
from app.proxy import get_proxy_client, proxy_with_auth
from app.sandbox_manager import SandboxManager
from app.ws_proxy import proxy_websocket_with_auth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("dshgw")

manager = SandboxManager()
_proxy_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _proxy_client
    settings = get_settings()
    manager.connect()
    manager.start_idle_gc()
    _proxy_client = get_proxy_client()
    logger.info(
        f"✓ dsh-gateway 就绪 auth_disabled={settings.auth_disabled} "
        f"user_image={settings.user_image}"
    )
    yield
    await _proxy_client.aclose()
    manager.close()


app = FastAPI(title="dsh-gateway", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_settings().session_secret_key or "dshgw-insecure-dev-key",
    same_site="lax",
)

app.include_router(auth.router, prefix="/dshgw")


@app.get("/dshgw/healthz")
async def healthz() -> dict:
    return {"ok": True}


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept


# 未登录时回退给浏览器的最小 PWA 清单(字段对齐 dsh 真实 manifest)
_FALLBACK_MANIFEST = {
    "name": "DeepSeek Harness",
    "short_name": "DSH",
    "start_url": "./",
    "scope": "./",
    "display": "fullscreen",
}


async def _resolve_user(
    request: Request,
) -> Union[str, Response]:
    """从网关会话解析用户;未登录时 HTML 请求跳登录页,其余 401。"""
    settings = get_settings()
    user_id = request.session.get("user_id")
    if user_id:
        return user_id
    if settings.auth_disabled:
        request.session["user_id"] = settings.default_dev_user_id
        return settings.default_dev_user_id
    if _wants_html(request):
        return RedirectResponse(url="/dshgw/auth/login", status_code=302)
    raise HTTPException(status_code=401, detail="未登录")


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def catch_all(request: Request, path: str) -> Response:
    # Chrome 的 PWA manifest 拉取不带凭据(<link rel="manifest"> 无
    # crossorigin 属性,登录用户也会以未登录面貌到达),直接 401 会在控制台
    # 刷错误;manifest 只是静态元数据,未登录时回退最小清单即可,
    # 带会话的请求仍走代理拿 dsh 真实 manifest
    if (
        path == "manifest.webmanifest"
        and request.method in ("GET", "HEAD")
        and not request.session.get("user_id")
        and not get_settings().auth_disabled
    ):
        return JSONResponse(
            _FALLBACK_MANIFEST, media_type="application/manifest+json"
        )
    user = await _resolve_user(request)
    if isinstance(user, Response):
        return user
    if path.startswith("dshgw/") or path == "dshgw":
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    # 展示名进沙盒管理器内存表:Pod 创建时注入 DSH_K8S_USERNAME
    # (镜像内 dsh-k8s-user-info 插件读取后展示在侧栏底部)
    manager.note_username(user, request.session.get("username") or user)
    try:
        return await proxy_with_auth(
            _proxy_client, request, manager, user, f"/{path}"
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"代理失败: user={user} path=/{path}")
        return JSONResponse({"detail": "沙盒暂不可用,请稍后重试"}, status_code=502)


# -------------------------------------------------------------- #
#  透传:WebSocket(dsh 流式通道 /api/remote.mux)                 #
# -------------------------------------------------------------- #


@app.websocket("/{path:path}")
async def catch_all_ws(ws: WebSocket, path: str) -> None:
    # SessionMiddleware 对 WS 握手同样解析 cookie(握手是 HTTP 请求,
    # 浏览器会带会话 cookie)
    user_id = None
    try:
        user_id = ws.session.get("user_id")
    except Exception:
        pass
    if not user_id and get_settings().auth_disabled:
        user_id = get_settings().default_dev_user_id
    if not user_id:
        await ws.accept()
        await ws.close(code=1008, reason="未登录")
        return

    try:
        manager.note_username(user_id, ws.session.get("username") or user_id)
    except Exception:
        pass  # dev 模式(auth_disabled)下可能没有会话对象
    manager.retain(user_id)
    try:
        handle = await asyncio.to_thread(manager.ensure_running, user_id)
        await proxy_websocket_with_auth(ws, manager, user_id, handle, f"/{path}")
    finally:
        manager.release(user_id)
