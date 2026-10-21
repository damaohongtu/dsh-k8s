"""K8s 沙盒编排 — 每用户一个 dsh Pod + 一个 PVC。

语义沿用 openclaw-k8s / opencode-k8s 的沙盒编排:
- Pod `dsh-user-<safe>` + PVC `dsh-user-<safe>`;PVC 永远保留(stop 只删 Pod)
- 归属校验:Pod / PVC 均带 io.dsh.user_id label,拒绝复用他人资源
- Pod IP 寻址(registry 记录 IP,Pod 重建后自动刷新)

dsh 原生保持的部分(零修改):
- 镜像与入口:官方无容器发行,自建镜像(image/)打包官方 npm 包
  @deepseek-ai/dsh,入口 dsh-entry 以 `dsh --profile web --host <PodIP>
  --port 3080` 启动(dsh 拒绝绑定通配地址;web UI 由 dsh 自己托管)
- 认证:dsh 原生是"启动时打印一次性 token URL,浏览器访问后换签名
  会话 cookie"。网关代浏览器完成交换(见 sandbox_manager),cookie 绑定
  交换时的 authority(PodIP:3080),网关代理保持同一 Host 即链路自洽
- 数据:HOME=/data(PVC),DSH_HOME=/data/.dsh(配置/凭据)与
  /data/workspace 全部落 PVC;目录骨架由 dsh-entry 以 node 用户创建
- 探针:tcpSocket 探 3080(dsh 只绑 PodIP,容器内探 127.0.0.1 不通;
  网关侧另有完整的 cookie 链路健康检查)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)


_LABEL_ROLE = "io.dsh.role"
_LABEL_USER = "io.dsh.user_id"

DSH_PORT = 3080

# 容器内挂载点:HOME=/data,DSH_HOME 与工作区全部落 PVC
_DATA_HOME = "/data"

# dsh web 启动时打印的登录 URL 行,形如:
#   dsh web: http://10.244.0.16:3080/?token=<43 字符>
_TOKEN_RE = re.compile(r"dsh web: \S*token=([A-Za-z0-9_\-]+)")


@dataclass(frozen=True)
class PodState:
    phase: str
    pod_ip: Optional[str]
    ready: bool


def _pod_state(pod) -> PodState:
    phase = pod.status.phase or "Unknown"
    pod_ip = (pod.status.pod_ip or "").strip() or None
    ready = False
    for cond in pod.status.conditions or []:
        if cond.type == "Ready":
            ready = bool(cond.status == "True")
            break
    return PodState(phase=phase, pod_ip=pod_ip, ready=ready)


def _k8s_quantity(value: str, default: str) -> str:
    """兼容 docker 风格后缀(2g/512m)→ K8s quantity(2Gi/512Mi)。"""
    v = (value or "").strip() or default
    return v.replace("g", "Gi").replace("m", "Mi")


class K8sSandboxClient:
    """通过 Kubernetes API 管理每用户 dsh Pod / PVC。"""

    def __init__(self, *, namespace: Optional[str] = None):
        self._namespace = namespace or get_settings().k8s_namespace
        self._core = None

    # -------------------------------------------------------------- #
    #  连接                                                           #
    # -------------------------------------------------------------- #

    def connect(self) -> None:
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:  # pragma: no cover - 仅静音告警
            pass

        try:
            from kubernetes import client as k8s_client
            from kubernetes import config as k8s_config

            try:
                k8s_config.load_incluster_config()
                logger.info("K8s 沙盒后端:使用 in-cluster ServiceAccount 凭据")
            except Exception:
                k8s_config.load_kube_config()
                logger.warning("K8s 沙盒后端:in-cluster 凭据不可用,回退本机 kubeconfig(仅限调试)")
            self._core = k8s_client.CoreV1Api()
            self._core.list_namespaced_pod(self._namespace, limit=1, _request_timeout=10)
        except Exception as e:
            self._core = None
            raise RuntimeError(
                f"Kubernetes API 不可用(namespace={self._namespace}),拒绝以 k8s 沙盒模式启动"
            ) from e
        logger.info(f"✓ K8s 沙盒后端就绪 namespace={self._namespace}")

    def close(self) -> None:
        self._core = None

    @property
    def namespace(self) -> str:
        return self._namespace

    # -------------------------------------------------------------- #
    #  PVC                                                            #
    # -------------------------------------------------------------- #

    def _ensure_pvc(self, user_id: str, safe: str) -> str:
        from kubernetes import client as k8s_client
        from kubernetes.client import ApiException

        settings = get_settings()
        name = f"dsh-user-{safe}"
        try:
            pvc = self._core.read_namespaced_persistent_volume_claim(name, self._namespace)
            owner = (pvc.metadata.labels or {}).get(_LABEL_USER)
            if owner and owner != user_id:
                raise RuntimeError("拒绝挂载其他用户的数据卷")
            if not owner:
                raise RuntimeError("无法确认旧数据卷归属,拒绝自动挂载")
            return name
        except ApiException as e:
            if e.status != 404:
                raise

        pvc = k8s_client.V1PersistentVolumeClaim(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                labels={_LABEL_ROLE: "user-data", _LABEL_USER: user_id},
            ),
            spec=k8s_client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                storage_class_name=settings.k8s_storage_class or None,
                resources=k8s_client.V1ResourceRequirements(
                    requests={"storage": _k8s_quantity(settings.k8s_user_storage, "5Gi")}
                ),
            ),
        )
        try:
            self._core.create_namespaced_persistent_volume_claim(self._namespace, pvc)
            logger.info(f"创建用户 PVC {name}({settings.k8s_user_storage})")
        except ApiException as e:
            if e.status != 409:  # 并发创建竞态:已存在则复用(重新读取并校验归属)
                raise
            created = self._core.read_namespaced_persistent_volume_claim(name, self._namespace)
            owner = (created.metadata.labels or {}).get(_LABEL_USER)
            if owner != user_id:
                raise RuntimeError("拒绝挂载其他用户的数据卷")
        return name

    # -------------------------------------------------------------- #
    #  Pod                                                            #
    # -------------------------------------------------------------- #

    def _get_pod(self, name: str):
        from kubernetes.client import ApiException

        try:
            return self._core.read_namespaced_pod(name, self._namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def _build_pod(self, user_id: str, safe: str, name: str, env: dict):
        from kubernetes import client as k8s_client

        settings = get_settings()
        try:
            cpus = float(settings.user_cpus or 1.0)
        except Exception:
            cpus = 1.0
        mem = _k8s_quantity(settings.user_mem, "2Gi")
        tmpfs = _k8s_quantity(settings.user_tmpfs_size, "1Gi")

        env_vars = [k8s_client.V1EnvVar(name=k, value=str(v)) for k, v in env.items()]
        pull_secrets = (
            [k8s_client.V1LocalObjectReference(name=settings.k8s_image_pull_secret)]
            if settings.k8s_image_pull_secret.strip()
            else []
        )

        container = k8s_client.V1Container(
            name="dsh",
            image=settings.user_image,
            image_pull_policy="IfNotPresent",
            # 入口在镜像 ENTRYPOINT(dsh-entry):hostname -i 取 Pod IP 传给 dsh --host;
            # 目录骨架(DSH_HOME/workspace)也由它以 node 用户创建
            env=env_vars,
            ports=[k8s_client.V1ContainerPort(container_port=DSH_PORT, name="http")],
            resources=k8s_client.V1ResourceRequirements(
                requests={"cpu": "100m", "memory": "512Mi"},
                limits={"cpu": cpus, "memory": mem},
            ),
            # dsh 只绑 Pod IP,容器内探 127.0.0.1 不通;tcpSocket 由 kubelet
            # 从节点连 PodIP:3080,与绑定方式天然匹配
            startup_probe=k8s_client.V1Probe(
                tcp_socket=k8s_client.V1TCPSocketAction(port="http"),
                period_seconds=5,
                failure_threshold=60,
            ),
            readiness_probe=k8s_client.V1Probe(
                tcp_socket=k8s_client.V1TCPSocketAction(port="http"),
                initial_delay_seconds=5,
                period_seconds=10,
            ),
            liveness_probe=k8s_client.V1Probe(
                tcp_socket=k8s_client.V1TCPSocketAction(port="http"),
                initial_delay_seconds=30,
                period_seconds=30,
            ),
            security_context=k8s_client.V1SecurityContext(
                allow_privilege_escalation=False,
                capabilities=k8s_client.V1Capabilities(drop=["NET_RAW", "NET_ADMIN"]),
            ),
            volume_mounts=[
                # HOME=/data:DSH_HOME(配置/凭据)+ 工作区全部落 PVC
                k8s_client.V1VolumeMount(name="data", mount_path=_DATA_HOME),
                k8s_client.V1VolumeMount(name="tmp", mount_path="/tmp"),
            ],
        )

        return k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                labels={
                    "app.kubernetes.io/name": "dsh-user",
                    "app.kubernetes.io/managed-by": "dsh-gateway",
                    _LABEL_ROLE: "user-sandbox",
                    _LABEL_USER: user_id,
                },
            ),
            spec=k8s_client.V1PodSpec(
                restart_policy="Always",
                image_pull_secrets=pull_secrets or None,
                service_account_name=None,
                # 非 root 运行 + fsGroup 让 PVC 可写;uid/gid 1000 = 镜像 node 用户
                security_context=k8s_client.V1PodSecurityContext(
                    run_as_user=1000,
                    run_as_group=1000,
                    fs_group=1000,
                ),
                containers=[container],
                volumes=[
                    k8s_client.V1Volume(
                        name="data",
                        persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=f"dsh-user-{safe}"
                        ),
                    ),
                    k8s_client.V1Volume(
                        name="tmp",
                        empty_dir=k8s_client.V1EmptyDirVolumeSource(medium="Memory", size_limit=tmpfs),
                    ),
                ],
            ),
        )

    def _wait_ready(self, name: str, *, timeout: float) -> PodState:
        from kubernetes.client import ApiException

        deadline = time.monotonic() + timeout
        last: Optional[PodState] = None
        while time.monotonic() < deadline:
            try:
                pod = self._get_pod(name)
            except ApiException as e:
                raise RuntimeError(f"读取 user Pod 失败: {name} ({e.reason})") from e
            if pod is None:
                raise RuntimeError(f"user Pod 意外消失: {name}")
            state = _pod_state(pod)
            last = state
            if state.ready and state.pod_ip:
                return state
            if state.phase in ("Failed", "Succeeded"):
                break
            time.sleep(1.0)
        phase = last.phase if last else "Unknown"
        raise RuntimeError(
            f"user Pod 未在 {int(timeout)}s 内就绪: {name} phase={phase} "
            f"(events: kubectl -n {self._namespace} describe pod {name})"
        )

    def _wait_deleted(self, name: str, *, timeout: float = 90.0) -> None:
        """等待 Pod 彻底删除(grace period + 镜像拉取取消都可能拖一会)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._get_pod(name) is None:
                return
            time.sleep(1.0)
        raise RuntimeError(f"等待旧 user Pod 删除超时: {name}")

    def ensure(self, user_id: str, safe: str, env: dict) -> str:
        """幂等启动 user Pod,返回可寻址的 Pod IP。"""
        from kubernetes.client import ApiException

        settings = get_settings()
        name = f"dsh-user-{safe}"
        self._ensure_pvc(user_id, safe)

        existing = self._get_pod(name)
        if existing is not None:
            labels = existing.metadata.labels or {}
            if labels.get(_LABEL_USER) != user_id:
                raise RuntimeError("拒绝复用不属于当前用户的容器")
            if existing.metadata.deletion_timestamp is not None:
                # 正在删除(如刚被 GC/手工 delete):等消失后重建
                self._wait_deleted(name)
            elif _pod_state(existing).phase in ("Failed", "Succeeded"):
                self._delete_pod(name)  # 终态 Pod 不会恢复,删除后走重建
                self._wait_deleted(name)
            else:
                state = self._wait_ready(name, timeout=settings.k8s_pod_ready_timeout)
                logger.info(f"复用 user Pod {name} ip={state.pod_ip}")
                return state.pod_ip

        pod = self._build_pod(user_id, safe, name, env)
        try:
            self._core.create_namespaced_pod(self._namespace, pod)
        except ApiException as e:
            if e.status != 409:
                raise
            logger.info(f"user Pod 已被并发创建: {name}")
        state = self._wait_ready(name, timeout=settings.k8s_pod_ready_timeout)
        logger.info(f"启动 user Pod user={user_id} name={name} ip={state.pod_ip}")
        return state.pod_ip

    def _delete_pod(self, name: str) -> None:
        from kubernetes.client import ApiException

        try:
            self._core.delete_namespaced_pod(name, self._namespace, grace_period_seconds=30)
        except ApiException as e:
            if e.status != 404:
                raise

    def stop(self, user_id: str, safe: str) -> bool:
        """删除 user Pod(PVC 保留)。归属不符时抛错。"""
        name = f"dsh-user-{safe}"
        pod = self._get_pod(name)
        if pod is None:
            return False
        labels = pod.metadata.labels or {}
        if labels.get(_LABEL_USER) != user_id:
            raise RuntimeError("拒绝停止归属不匹配的容器")
        self._delete_pod(name)
        return True

    def is_running(self, user_id: str, safe: str) -> bool:
        name = f"dsh-user-{safe}"
        pod = self._get_pod(name)
        if pod is None:
            return False
        return pod.metadata.deletion_timestamp is None and _pod_state(pod).phase in (
            "Pending",
            "Running",
        )

    # -------------------------------------------------------------- #
    #  启动 token(认证代换的原料)                                     #
    # -------------------------------------------------------------- #

    def read_startup_token(self, pod_name: str, *, timeout: float = 60.0) -> str:
        """轮询 Pod 日志,返回最近一次 dsh web 打印的启动 token。

        dsh 每次进程启动都打印新的 token 行(token 可重复用于交换);
        Pod Ready 后行几乎立即出现,这里留一个轮询窗口容忍启动抖动。
        """
        deadline = time.monotonic() + timeout
        last_err: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                log = self._core.read_namespaced_pod_log(pod_name, self._namespace)
                match = None
                for match in _TOKEN_RE.finditer(log or ""):
                    pass
                if match:
                    return match.group(1)
                last_err = RuntimeError("日志中尚无 dsh web token 行(dsh 仍在启动)")
            except Exception as e:  # 日志暂时不可读(容器重启中等)
                last_err = e
            time.sleep(1.0)
        raise RuntimeError(f"读取启动 token 超时: {pod_name} ({last_err})")
