"""dsh 沙盒管理器 — SQLite 注册表 + 生命周期 + dsh 认证代换。

沿用 openclaw-k8s / opencode-k8s 的管理器形态:
- 单副本 + Recreate 部署,注册表落网关自己的 PVC(SQLite);多副本 = 脑裂
- ensure_running 幂等:命中健康缓存直接返回,否则确保 Pod Ready 并登记
- retain / release 引用计数保护在途请求;闲置 GC 只删 Pod,PVC 永远保留

dsh 特有:认证代换(替代 opencode 的 per-user password 注入)。
dsh 原生认证 = 启动时打印 token URL,浏览器访问后换签名会话 cookie;
网关代浏览器完成交换并缓存 cookie,代理时注入。失效自愈链:
  请求 401 → 失效缓存 → 重读日志最后一条 token 行 → 重新交换 → 重放一次
  仍 401 → SandboxAuthMismatch → 删 Pod 重建(终局自愈)
cookie 绑定交换时的 authority(PodIP:3080),网关代理保持同一 Host,
链路自洽,无需改写 dsh 的任何校验。

本模块全部为同步阻塞实现(K8s client / sqlite / httpx 同步客户端),
FastAPI 侧用 asyncio.to_thread 调用,避免阻塞事件循环。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from app.config import get_settings
from app.safe_user import safe_user_id
from app.sandbox_k8s import DSH_PORT, K8sSandboxClient

logger = logging.getLogger(__name__)

_GC_INTERVAL = 60.0


class SandboxAuthMismatch(RuntimeError):
    """重新获取的认证仍被 dsh 拒绝 —— 需要删 Pod 重建。"""

    def __init__(self, user_id: str):
        super().__init__(f"dsh 认证持续失败,需要重建沙盒: {user_id}")
        self.user_id = user_id


@dataclass(frozen=True)
class SandboxHandle:
    user_id: str
    safe_user_id: str
    pod_name: str
    internal_host: str
    internal_port: int
    status: str
    last_active_at: float

    @property
    def base_url(self) -> str:
        return f"http://{self.internal_host}:{self.internal_port}"


@dataclass(frozen=True)
class DshAuth:
    """一个用户沙盒的网关侧 dsh 会话。"""

    user_id: str
    pod_name: str
    pod_ip: str
    token: str
    cookie_header: str  # 形如 "dsh-auth-xxxx=<签名>",注入代理请求


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandbox_registry (
    user_id        TEXT PRIMARY KEY,
    safe_user_id   TEXT NOT NULL,
    pod_name       TEXT NOT NULL,
    internal_host  TEXT NOT NULL,
    internal_port  INTEGER NOT NULL,
    status         TEXT NOT NULL,
    last_active_at REAL NOT NULL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
)
"""


class SandboxManager:
    def __init__(self):
        settings = get_settings()
        self._settings = settings
        self._client = K8sSandboxClient()
        self._db_path = settings.registry_db
        self._global_lock = threading.RLock()
        self._user_locks: dict[str, threading.RLock] = {}
        # user_id -> (handle, expires_at):健康句柄缓存,避免每请求都查 K8s API
        self._healthy: dict[str, tuple[SandboxHandle, float]] = {}
        # user_id -> DshAuth:dsh 会话 cookie 缓存
        self._auth_cache: dict[str, DshAuth] = {}
        # user_id -> 在途请求数:GC 不回收有在途请求的用户
        self._active_requests: dict[str, int] = {}
        # user_id -> CAS 展示名:每请求刷新;Pod 创建时注入 DSH_K8S_USERNAME,
        # 由镜像内 dsh-k8s-user-info 插件在侧栏底部展示(见 image/dsh-k8s-user-info)
        self._usernames: dict[str, str] = {}
        self._gc_thread: Optional[threading.Thread] = None
        self._gc_stop = threading.Event()

    # -------------------------------------------------------------- #
    #  生命周期                                                        #
    # -------------------------------------------------------------- #

    def connect(self) -> None:
        settings = self._settings
        if settings.sandbox_mode == "k8s" and not settings.session_secret_key:
            raise RuntimeError(
                "k8s 沙盒模式必须设置 DSHGW_SESSION_SECRET_KEY"
                "(openssl rand -hex 32;见 k8s/30-secret.example.yaml)"
            )
        db_dir = Path(self._db_path).parent
        if str(db_dir):
            db_dir.mkdir(parents=True, exist_ok=True)
        with self._connect_db() as conn:
            conn.executescript(_SCHEMA)
        self._client.connect()
        logger.info(
            f"✓ 沙盒管理器就绪 mode={settings.sandbox_mode} "
            f"namespace={self._client.namespace} db={self._db_path}"
        )

    def close(self) -> None:
        self._gc_stop.set()
        self._client.close()

    def start_idle_gc(self) -> None:
        self._gc_thread = threading.Thread(
            target=self._gc_loop, name="dshgw-idle-gc", daemon=True
        )
        self._gc_thread.start()
        logger.info(
            f"✓ 闲置回收已启动 interval={int(_GC_INTERVAL)}s "
            f"idle_ttl={self._settings.sandbox_idle_ttl}s(仅删 Pod,PVC 保留)"
        )

    def _user_lock(self, user_id: str) -> threading.RLock:
        with self._global_lock:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = threading.RLock()
                self._user_locks[user_id] = lock
            return lock

    # -------------------------------------------------------------- #
    #  注册表(sqlite)                                                 #
    # -------------------------------------------------------------- #

    def _connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _upsert(self, handle: SandboxHandle) -> None:
        now = time.time()
        with self._connect_db() as conn:
            conn.execute(
                """
                INSERT INTO sandbox_registry
                    (user_id, safe_user_id, pod_name, internal_host, internal_port,
                     status, last_active_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    safe_user_id=excluded.safe_user_id,
                    pod_name=excluded.pod_name,
                    internal_host=excluded.internal_host,
                    internal_port=excluded.internal_port,
                    status=excluded.status,
                    last_active_at=excluded.last_active_at,
                    updated_at=excluded.updated_at
                """,
                (
                    handle.user_id, handle.safe_user_id, handle.pod_name,
                    handle.internal_host, handle.internal_port,
                    handle.status, handle.last_active_at, now, now,
                ),
            )

    def _touch(self, user_id: str) -> None:
        now = time.time()
        with self._connect_db() as conn:
            conn.execute(
                "UPDATE sandbox_registry SET last_active_at=?, updated_at=? WHERE user_id=?",
                (now, now, user_id),
            )

    def _set_status(self, user_id: str, status: str) -> None:
        with self._connect_db() as conn:
            conn.execute(
                "UPDATE sandbox_registry SET status=?, updated_at=? WHERE user_id=?",
                (status, time.time(), user_id),
            )

    def _list_rows(self) -> list[sqlite3.Row]:
        with self._connect_db() as conn:
            return list(conn.execute("SELECT * FROM sandbox_registry").fetchall())

    # -------------------------------------------------------------- #
    #  ensure / retain / release / stop                                #
    # -------------------------------------------------------------- #

    def note_username(self, user_id: str, username: str) -> None:
        """记录用户的 CAS 展示名(每请求调用;仅内存,网关重启后由下一请求补上)。"""
        if username:
            self._usernames[user_id] = username

    def _pod_env(self, user_id: str) -> dict:
        env = {}
        if self._settings.deepseek_api_key.strip():
            # dsh 原生环境变量:预置默认模型凭据;留空则用户在 web UI 自行配置
            env["DEEPSEEK_API_KEY"] = self._settings.deepseek_api_key.strip()
        # 平台身份:镜像内 dsh-k8s-user-info 插件读取后展示在侧栏底部
        env["DSH_K8S_USER_ID"] = user_id
        username = self._usernames.get(user_id)
        if username:
            env["DSH_K8S_USERNAME"] = username
        return env

    def ensure_running(self, user_id: str) -> SandboxHandle:
        """幂等确保用户沙盒在运行,返回可寻址句柄(每请求都会调用,顺带 touch)。"""
        with self._user_lock(user_id):
            now = time.time()
            cached = self._healthy.get(user_id)
            if cached is not None and cached[1] > now:
                self._touch(user_id)
                return cached[0]

            safe = safe_user_id(user_id)
            pod_ip = self._client.ensure(user_id, safe, self._pod_env(user_id))
            handle = SandboxHandle(
                user_id=user_id,
                safe_user_id=safe,
                pod_name=f"dsh-user-{safe}",
                internal_host=pod_ip,
                internal_port=DSH_PORT,
                status="running",
                last_active_at=now,
            )
            self._upsert(handle)
            self._healthy[user_id] = (handle, now + self._settings.sandbox_health_ttl)
            return handle

    def retain(self, user_id: str) -> None:
        with self._global_lock:
            self._active_requests[user_id] = self._active_requests.get(user_id, 0) + 1

    def release(self, user_id: str) -> None:
        with self._global_lock:
            remaining = self._active_requests.get(user_id, 0) - 1
            if remaining <= 0:
                self._active_requests.pop(user_id, None)
            else:
                self._active_requests[user_id] = remaining

    def stop(self, user_id: str) -> bool:
        """删除用户 Pod(PVC 保留)。"""
        with self._user_lock(user_id):
            safe = safe_user_id(user_id)
            self._auth_cache.pop(user_id, None)
            self._healthy.pop(user_id, None)
            stopped = self._client.stop(user_id, safe)
            if stopped:
                self._set_status(user_id, "stopped")
                logger.info(f"已回收用户沙盒 Pod(PVC 保留): {user_id}")
            return stopped

    def recreate(self, user_id: str) -> SandboxHandle:
        """终局自愈:删 Pod 重建(数据在 PVC,不丢)。"""
        with self._user_lock(user_id):
            logger.warning(f"认证持续失败,重建用户沙盒: {user_id}")
            self.stop(user_id)
            return self.ensure_running(user_id)

    # -------------------------------------------------------------- #
    #  dsh 认证代换                                                    #
    # -------------------------------------------------------------- #

    def get_valid_auth(self, user_id: str, handle: SandboxHandle) -> DshAuth:
        """返回可用的 dsh 会话;缓存未失效则直接用,否则重新代换。

        缓存命中不做主动校验(避免每请求多一跳),失效靠代理层的
        401 自愈链:invalidate_auth → 下次 get_valid_auth 重新代换。
        """
        with self._user_lock(user_id):
            cached = self._auth_cache.get(user_id)
            if cached is not None and cached.pod_ip == handle.internal_host:
                return cached
            return self._acquire_auth(user_id, handle)

    def invalidate_auth(self, user_id: str) -> None:
        with self._user_lock(user_id):
            self._auth_cache.pop(user_id, None)

    def _acquire_auth(self, user_id: str, handle: SandboxHandle) -> DshAuth:
        token = self._client.read_startup_token(handle.pod_name)
        cookie = self._exchange_token(handle.internal_host, token)
        auth = DshAuth(
            user_id=user_id,
            pod_name=handle.pod_name,
            pod_ip=handle.internal_host,
            token=token,
            cookie_header=cookie,
        )
        if not self._validate_auth(auth):
            raise RuntimeError(
                f"dsh token 交换成功但会话校验失败: {user_id} "
                f"(pod={handle.pod_name})"
            )
        self._auth_cache[user_id] = auth
        logger.info(f"已建立 dsh 会话(代换): user={user_id} pod={handle.pod_name}")
        return auth

    def _exchange_token(self, pod_ip: str, token: str) -> str:
        """访问 dsh 的 token URL 换会话 cookie。

        返回 303 + Set-Cookie(cookie 名每次安装随机,如 dsh-auth-xxxxx);
        Host 由 httpx 自动置为 PodIP:3080,与 cookie 绑定的 authority 一致。
        """
        url = f"http://{pod_ip}:{DSH_PORT}/"
        try:
            with httpx.Client(
                follow_redirects=False, trust_env=False, timeout=15.0
            ) as client:
                resp = client.get(url, params={"token": token})
        except httpx.HTTPError as e:
            raise RuntimeError(f"dsh token 交换请求失败: {e}") from e
        set_cookies = resp.headers.get_list("set-cookie")
        if not set_cookies:
            raise RuntimeError(
                f"dsh token 交换未返回会话 cookie(status={resp.status_code})"
            )
        cookie = set_cookies[0].split(";")[0].strip()
        if "=" not in cookie:
            raise RuntimeError(f"dsh 返回的会话 cookie 格式异常: {cookie!r}")
        return cookie

    def _validate_auth(self, auth: DshAuth) -> bool:
        """带 cookie GET / :200 = 会话有效,401 = 失效,网络错误 = 不可达。"""
        try:
            with httpx.Client(
                follow_redirects=False, trust_env=False, timeout=10.0
            ) as client:
                resp = client.get(
                    f"http://{auth.pod_ip}:{DSH_PORT}/",
                    headers={"cookie": auth.cookie_header},
                )
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    # -------------------------------------------------------------- #
    #  闲置回收                                                        #
    # -------------------------------------------------------------- #

    def _gc_loop(self) -> None:
        while not self._gc_stop.wait(_GC_INTERVAL):
            try:
                self._run_gc_pass()
            except Exception:
                logger.exception("闲置回收本轮执行失败(下轮重试)")

    def _run_gc_pass(self) -> None:
        settings = self._settings
        cutoff = time.time() - settings.sandbox_idle_ttl
        for row in self._list_rows():
            user_id = row["user_id"]
            if row["status"] != "running":
                continue
            with self._global_lock:
                if user_id in self._active_requests:
                    continue
            if row["last_active_at"] >= cutoff:
                continue
            idle = int(time.time() - row["last_active_at"])
            logger.info(f"回收闲置沙盒: {user_id}(闲置 {idle}s ≥ TTL {settings.sandbox_idle_ttl}s)")
            self.stop(user_id)
