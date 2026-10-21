"""dsh-gateway 配置 — 环境变量前缀 DSHGW_(DeepSeek Harness GateWay)。

命名空间划分:
- DSHGW_SANDBOX_* / DSHGW_K8S_*  沙盒编排
- DSHGW_USER_*                  每用户 dsh Pod 的资源与寻址
- CAS_* / AUTH_DISABLED         认证
- DEEPSEEK_API_KEY              透传给用户 dsh 的模型凭据(原生环境变量)
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_cas_server_url(url: str) -> str:
    """规范化 CAS 服务地址:去尾部空白、补尾部 /(拼接 login/serviceValidate 用)。"""
    u = (url or "").strip().rstrip("/")
    return f"{u}/" if u else u


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DSHGW_", extra="ignore")

    # ---------- 沙盒编排 ---------- #
    sandbox_mode: str = "k8s"          # k8s(拉 Pod;唯一实现的模式)
    k8s_namespace: str = "dsh-system"
    k8s_storage_class: str = ""        # 空 = 集群默认 StorageClass
    k8s_image_pull_secret: str = ""    # 私有仓库时网关会把它注入用户 Pod
    k8s_pod_ready_timeout: int = 180   # dsh Pod 首次拉镜像 + 启动的窗口
    k8s_user_storage: str = "5Gi"      # 每用户 PVC(DSH_HOME + 工作区)

    # ---------- 用户 dsh Pod ---------- #
    # 端口固定 3080(sandbox_k8s.DSH_PORT,已固化进入口脚本与探针)
    user_image: str = "damaohongtu/dsh:0.2.1-rc.2"
    user_mem: str = "2Gi"
    user_cpus: str = "1.0"
    user_tmpfs_size: str = "1Gi"       # /tmp 走 tmpfs,agent 编译类任务用它
    sandbox_idle_ttl: int = 900        # 闲置秒数,超过回收 Pod(PVC 保留)
    sandbox_health_ttl: int = 15       # 健康句柄缓存秒数
    registry_db: str = "/data/dshgw/sandbox_registry.db"  # 本地调试可指到可写目录

    # ---------- 认证 ---------- #
    auth_disabled: bool = False
    default_dev_user_id: str = "dev"
    cas_version: str = "3"
    cas_server_url: str = ""
    cas_service_url: str = ""
    frontend_url: str = "/"            # 登录成功后的落地页(默认网关自身根路径)
    session_secret_key: str = ""

    # ---------- 模型凭据(注入用户 Pod;可留空,用户在 web UI 自行配置) ---------- #
    deepseek_api_key: str = ""

    @property
    def cas_server_url_normalized(self) -> str:
        return normalize_cas_server_url(self.cas_server_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
