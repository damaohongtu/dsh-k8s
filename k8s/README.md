# dsh-k8s 部署指南

登录用户自动获得**专属原生 DeepSeek Harness (dsh) 实例**（每用户一个 Pod + 一个 PVC），网关只做四件事：CAS 认证、Pod/PVC 编排、**认证代换**（dsh 启动 token → 会话 cookie）、HTTP 反向代理。web UI、agent 执行、模型配置全部由用户自己的 dsh 原生提供。

## 架构

```
浏览器 ──http://localhost:19092──┐
                                 ▼
                    ┌─────────────────────────┐
                    │  kind: dsh               │
                    │  ┌────────────────────┐  │
   NodePort 30080 ──┼─▶│ dsh-gateway        │  │  FastAPI(单副本)
                    │  │ CAS 认证 / 编排 /  │  │  SQLite 注册表(自身 PVC)
                    │  │ 认证代换 / HTTP 代理│  │
                    │  └─────┬──────────────┘  │
                    │        │ Pod IP:3080     │
                    │  ┌─────▼──────┐ ┌───────┐│  每用户 1 Pod
                    │  │ dsh-user-  │ │dsh-   ││  + 1 PVC(HOME=/data)
                    │  │ <id>       │ │user-… ││
                    │  └────────────┘ └───────┘│
   NodePort 31447 ──┼─▶ CAS(test1/123456)      │
                    └─────────────────────────┘
```

- **网关**（`dsh-gateway`）：CAS 登录 → 按需拉起用户 Pod → 读 Pod 日志拿 dsh 启动 token、代浏览器换会话 cookie → 把请求代理到 `PodIP:3080`（注入 cookie，Host 保持 `PodIP:3080`）；闲置 15 分钟回收 Pod（PVC 保留，数据不丢）
- **用户 Pod**（`dsh-user-<safe-id>`）：自建镜像（`image/`，打包官方 npm 包 `@deepseek-ai/dsh`）原样运行，入口 `dsh-entry` 执行 `dsh --profile web --host <PodIP> --port 3080`（dsh 拒绝绑定 0.0.0.0，必须绑容器自身 IP）
- **认证代换（dsh 与 opencode 的关键差异）**：dsh 原生认证是"启动打印 token URL → 浏览器访问换签名 cookie"，cookie 绑定 `PodIP:3080` 这个 authority。网关代浏览器完成交换并缓存 cookie，代理时注入；dsh 会话密钥持久化在 PVC（`.dsh/.credentials.yaml`），容器重启 cookie 依然有效，密钥轮换后 401 自愈（重读日志 token 重新代换，详见根 README）

## 前置条件

- Docker Desktop（≥ 4GB 可用内存）
- kind、kubectl

## 部署步骤

### 1. 生成凭据

```bash
cd k8s
./gen-secret.sh                # 生成 30-secret.yaml（已被 gitignore）
# 可选：预置模型凭据（留空则用户在 dsh web UI 自行配置，原生方式）
DEEPSEEK_API_KEY=sk-... ./gen-secret.sh
```

### 2. 构建镜像并创建集群

```bash
# dsh 用户镜像（官方 npm 包 + 入口脚本）
docker build -t damaohongtu/dsh:0.2.1-rc.2 image/

# 网关镜像
docker build -t damaohongtu/dsh-gateway:latest gateway/

kind create cluster --config k8s/kind-dsh.yaml
```

### 3. 导入镜像

Docker Desktop 下**远端多架构镜像**走 `kind load docker-image` 会失败
（`ctr: content digest not found`：docker 只保留本机平台的层，kind 按全平台校验）。
统一用 `docker save | ctr import`：

```bash
# CAS 镜像（远端多架构，先经镜像站拉取）
docker pull docker.m.daocloud.io/apereo/cas:6.6.10
docker tag docker.m.daocloud.io/apereo/cas:6.6.10 apereo/cas:6.6.10

# 本地构建的单平台镜像（dsh / 网关）与 CAS 一样导入即可
for img in damaohongtu/dsh:0.2.1-rc.2 damaohongtu/dsh-gateway:latest apereo/cas:6.6.10; do
  docker save "$img" | docker exec -i dsh-control-plane ctr --namespace=k8s.io images import -
done

# 验证已导入
docker exec dsh-control-plane crictl images
```

> 40-gateway.yaml 中 `DSHGW_USER_IMAGE` 必须与导入镜像名一致。
> CAS 镜像若导入失败（本地镜像存储缺层），可从任一已有该镜像的 kind 节点直灌：
> `docker exec <其他集群节点> ctr -n k8s.io images export - docker.io/apereo/cas:6.6.10 | docker exec -i dsh-control-plane ctr -n k8s.io images import -`

### 4. 应用清单

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/10-rbac.yaml
kubectl apply -f k8s/20-storage.yaml
kubectl apply -f k8s/25-user-netpol.yaml
kubectl apply -f k8s/30-secret.yaml
kubectl apply -f k8s/40-gateway.yaml
kubectl apply -f k8s/50-cas.yaml
kubectl -n dsh-system rollout status deploy/dsh-gateway --timeout=300s
```

### 5. 本机 hosts（浏览器可达 CAS）

```bash
sudo sh -c 'echo "127.0.0.1 cas.dsh-system.svc.cluster.local" >> /etc/hosts'
```

浏览器与 Pod 必须用**同一 URL** 访问 CAS：浏览器经 hosts + NodePort 31447，Pod 经集群 DNS + 同一 NodePort（50-cas.yaml 用 `port == nodePort` 保证两边一致）。

### 6. 验证端到端

```bash
kubectl -n dsh-system get pods -w
```

浏览器打开 <http://localhost:19092/>：

1. 跳转 CAS → 输入 `test1` / `123456` → 回跳
2. 网关按需拉起 `dsh-user-*` Pod（首次约 15 秒：PVC + Pod + token 代换）
3. 回跳后**自动进入**原生 dsh web UI；`DEEPSEEK_API_KEY` 已预置时直接发消息，否则在 UI 里配置模型（dsh 原生方式，凭据落自己的 PVC）
4. 侧栏底部可见 CAS 身份 chip（首字母圆圈；展开侧栏后显示用户名）

命令行验证（无浏览器，`--resolve` 代替 hosts）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:19092/dshgw/healthz   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:19092/                # 401(未登录)
```

## 配置参考（40-gateway.yaml 环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DSHGW_USER_IMAGE` | `damaohongtu/dsh:0.2.1-rc.2` | 用户 Pod 镜像（与导入镜像名一致） |
| `DSHGW_K8S_USER_STORAGE` | `5Gi` | 每用户 PVC 容量 |
| `DSHGW_USER_MEM` / `DSHGW_USER_CPUS` | `2Gi` / `1.0` | 用户 Pod 资源限额 |
| `DSHGW_USER_TMPFS_SIZE` | `1Gi` | 用户 Pod `/tmp` tmpfs 上限 |
| `DSHGW_SANDBOX_IDLE_TTL` | `900` | 闲置秒数后回收 Pod（PVC 保留） |
| `DSHGW_CAS_SERVER_URL` | 集群内 CAS | 浏览器与 Pod 都需可达 |
| `DSHGW_CAS_SERVICE_URL` | `http://localhost:19092/dshgw/auth/callback` | CAS 回调地址 |
| `DSHGW_FRONTEND_URL` | `http://localhost:19092/` | 登录后落地页 |
| `DSHGW_AUTH_DISABLED` | `False` | 调试模式：跳过 CAS 固定用户 `dev` |

Secret 键（30-secret.yaml，均带 `DSHGW_` 前缀经 envFrom 注入）：

- `DSHGW_SESSION_SECRET_KEY` — 网关会话签名，`openssl rand -hex 32`；重生成 = 所有 CAS 登录失效（不影响 dsh 沙盒内的用户数据）
- `DSHGW_DEEPSEEK_API_KEY` — 可选，注入用户 Pod 的默认模型凭据（dsh 原生环境变量 `DEEPSEEK_API_KEY`，只进 Pod env 不落 PVC）

## 设计要点（为什么这样保持原生）

- **单副本 + Recreate**：网关注册表是 PVC 上的 SQLite，多副本脑裂
- **认证代换（dsh 原生模式）**：dsh 自己做 token 校验和 cookie 签名，网关只是"读过日志、换过 cookie 的反代"；代理保持 `Host: PodIP:3080`（httpx 按目标 URL 自动设置），与 cookie 绑定的 authority 一致
- **两级自愈**：代理收到 401 → 失效缓存 → 重读日志最后一条 token 行（token 可重复用于交换）→ 重新代换 → 重放一次；仍 401 → 删 Pod 重建（PVC 数据不丢）
- **tcpSocket 探针**：dsh 只绑 Pod IP，容器内探 127.0.0.1 不通；kubelet 从节点连 `PodIP:3080` 与绑定方式天然匹配
- **HOME=/data**：DSH_HOME（`.dsh/` 配置/凭据，含浏览器会话密钥）与 `/data/workspace` 工作区全部落 PVC；镜像 `WORKDIR /data`、入口脚本以 node 用户建 workspace（规避 runc 把缺失 WORKDIR 建成 root 属主的陷阱）
- **PVC 永不删除**：回收只删 Pod；用户在 UI 里改的配置、凭据、工作区文件跨重启完整保留
- **请求体重放**：≤ 32MiB 的请求体网关缓冲以支持 401 重放；超限纯流式（不重放）
- **WS 桥接**（`ws_proxy.py`）：dsh 的流式通道 `/api/remote.mux` 承载所有 Remote 流与 `$events` 事件，网关注入同样的 dsh cookie 做纯透传桥接；上游握手 401 走与 HTTP 路径同款的两级自愈，连接期间 retain 防止闲置 GC 回收
- **剥离浏览器上下文头**：dsh 拒绝带 `Origin` 的请求（POST 一律 403），网关→dsh 的集群内调用剥掉 `origin` / `referer` / `sec-fetch-*`；CSRF 边界由"唯一入口是网关"承担
- **manifest 未登录回退**：Chrome 的 manifest 拉取不带凭据，未登录请求 `/manifest.webmanifest` 回退最小 PWA 清单（避免控制台 401 噪音），带会话仍走代理
- **平台身份展示（Cordis 插件 dsh-k8s-user-info）**：dsh 的账号区硬编码为 DeepSeek 官方账号体系，CAS 身份由镜像内置插件原生化展示在侧栏底部。链路：网关创建 Pod 时注入 env（`DSH_K8S_USER_ID` / `DSH_K8S_USERNAME`，展示名来自每请求刷新的内存表）→ `dsh-entry` 把插件从 `/opt` 复制到 `$DSH_HOME/node_modules` 并以 `--patch` 挂载其 `patch.yml` 启用 → Host 半监听 `webserver/index-inject` 把身份序列化为 `__DSH_K8S_USER__` 启动全局 → 浏览器半注册 `sidebar.footer.action` 槽渲染首字母圆圈 + 用户名（侧栏折叠时仅圆圈，与原生行为一致）。环境缺失（非网关部署）时插件不生效
- **dsh 本体零修改**：镜像内 `@deepseek-ai/dsh` 全部文件与官方 npm tarball sha256 一致；`--patch` 是 launcher 级 CLI 叠加层（不写入 profile 的 cordis.patch.yml）；网关对响应体零改动、逐字节透传（只做代理层标准头过滤）
- **NetworkPolicy 隔离用户 Pod**（25-user-netpol.yaml）：ingress 只放行网关；kind 默认 CNI 不执行，生产集群生效
- **RBAC 含 pods/log**：认证代换需要读用户 Pod 日志取 token 行，仅授予网关 SA、仅本 namespace

## 升级 dsh 版本

改 `image/Dockerfile` 的 `DSH_VERSION` 重新构建镜像、更新 `DSHGW_USER_IMAGE`，然后删除存量用户 Pod 让其重建（PVC 数据保留）：

```bash
kubectl -n dsh-system delete pod -l io.dsh.role=user-sandbox
```

## 排障

```bash
# 网关日志（CAS/PVC/Pod 编排/认证代换问题）
kubectl -n dsh-system logs deploy/dsh-gateway --tail=100

# 用户 Pod 未拉起
kubectl -n dsh-system describe pod -l io.dsh.role=user-sandbox

# dsh 启动失败（看 token 行是否打印）
kubectl -n dsh-system logs -l io.dsh.role=user-sandbox --tail=50

# 手动触发某用户沙盒重建（数据在 PVC，不丢）
kubectl -n dsh-system delete pod -l io.dsh.user_id=test1
```

常见问题：

- **网关日志 `已建立 dsh 会话(代换)` 频繁出现**：正常——会话密钥轮换/容器重启后自动重新代换
- **网关日志 `认证持续失败,重建用户沙盒` 后恢复**：正常——代换仍被拒时的终局自愈
- **登录后长时间空白页**：首次 Pod 启动约 15 秒（PVC + Pod + token 代换），看 `kubectl -n dsh-system get pods -w`；Ready 后刷新即可
- **CAS 回调 401**：检查 hosts 配置与 `DSHGW_CAS_SERVICE_URL` 是否精确一致
- **回收后又立即被拉起**：正常——浏览器还开着 web UI，关页面即闲置
- **网关 Pod CrashLoop 且日志提示 SESSION_SECRET_KEY**：`k8s/gen-secret.sh` 没跑或 Secret 未应用
