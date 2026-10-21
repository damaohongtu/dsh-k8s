# dsh-k8s

CAS 登录后，为每个用户自动拉起一个**原生 DeepSeek Harness (dsh) 实例**（每用户 1 Pod + 1 PVC）跑在 Kubernetes 上，浏览器直接使用 dsh 官方 web UI。

参考 [openclaw-k8s](../openclaw-k8s) / [opencode-k8s](../opencode-k8s) 的"登录 → 拉起容器"模式；dsh 本体零修改：官方 npm 包（`@deepseek-ai/dsh`）+ 官方 CLI 参数（`dsh --profile web`）+ 官方环境变量（`DEEPSEEK_API_KEY`），web UI 由 dsh 自己托管。

## 架构

```
浏览器 ──http://localhost:19092──┐
                                 ▼
                    ┌─────────────────────────┐
                    │  kind: dsh               │
                    │  ┌────────────────────┐  │
   NodePort 30080 ──┼─▶│ dsh-gateway        │  │  FastAPI(单副本)
                    │  │ CAS 认证 / 编排 /  │  │  SQLite 注册表(自身 PVC)
                    │  │ 认证代换 / 代理    │  │  HTTP 反代 + WS 桥接
                    │  └─────┬──────────────┘  │
                    │        │ Pod IP:3080     │
                    │  ┌─────▼──────┐ ┌───────┐│  每用户 1 Pod
                    │  │ dsh-user-  │ │dsh-   ││  + 1 PVC(HOME=/data)
                    │  │ <id>       │ │user-… ││
                    │  └────────────┘ └───────┘│
   NodePort 31447 ──┼─▶ CAS(test1/123456)      │
                    └─────────────────────────┘
```

- **网关**（`gateway/`）：CAS 单点登录 → 按需创建用户 Pod → **认证代换** → HTTP 反向代理 + WebSocket 桥接，并把 CAS 身份经 Pod 环境注入 dsh（见下节"Cordis 插件"）；闲置 15 分钟回收 Pod，PVC 保留
- **用户 Pod**（`image/` 自建镜像，打包官方 npm 包）：`dsh --profile web --host <PodIP> --port 3080`，web UI 由 dsh 自己托管
- **隔离**：每用户独立 PVC（HOME=/data：DSH_HOME 配置/凭据、工作区）+ NetworkPolicy（ingress 仅放行网关）+ 归属校验（拒绝挂载他人数据卷）

## 平台身份展示：Cordis 插件 dsh-k8s-user-info

dsh 的账号区硬编码为 DeepSeek 官方账号体系，CAS 身份进不去其原生账号 UI。自建 Cordis 插件（`image/dsh-k8s-user-info/`）把平台身份原生化地展示在侧栏底部：

```
网关 ──Pod env: DSH_K8S_USER_ID / DSH_K8S_USERNAME──▶ dsh-entry
  ├─ 每次启动把插件从 /opt/dsh-k8s-user-info 复制到 $DSH_HOME/node_modules
  │   （loader 从 profile 目录链解析包名；PVC 上 pnpm 工作区之外，
  │   dsh plugin add 不会动它）
  └─ dsh --patch <插件>/patch.yml 启用（--patch 是 launcher 级选项，必须在 app 参数之前）
        ├─ Host 半（lib/index.js）：patch.yml 以 !!js 读环境 → 监听 webserver/index-inject
        │   → HTML 内联 globalThis.__DSH_K8S_USER__ = {userId, username}
        └─ 浏览器半（lib/client.js，window.__ModuleLoader__ 预构建格式）：
            注册 sidebar.footer.action 槽 → 渲染首字母圆圈 + 用户名
```

- **侧栏折叠（icon rail）时只显示首字母圆圈，展开（wide）时显示用户名**——与 dsh 原生侧栏行为一致，主题安全（currentColor）
- 用户名由网关内存表提供（每请求 `note_username()` 刷新；重启后由下一请求补上），Pod 创建时注入 env；环境缺失（非网关部署）时插件整体不生效，dsh 原生行为不受影响
- 插件不声明 Config schema：schemastery 从 PVC 位置解析不到，无 Config 时 Cordis 原样透传配置对象

**dsh 本体零修改**（已审计）：镜像内 `@deepseek-ai/dsh` 全部 20 个文件与官方 npm tarball 逐字节一致（sha256 比对）；`--patch` 是 launcher 级 CLI 叠加层，不写入 profile 的 cordis.patch.yml；PVC 上唯一新增是 `DSH_HOME/node_modules/dsh-k8s-user-info/`；插件 Host/浏览器半只调用官方扩展点（`webserver/index-inject` 事件、slots 注册），无 monkey-patch；网关对响应体零改动、逐字节透传。

## dsh 的认证差异：token 代换

opencode v2 有原生 HTTP Basic auth，网关注入 `Authorization` 头即可；**dsh 的原生认证是另一套**（官方面向"本机启动 → 打印 URL → 浏览器打开"的流程）：

1. dsh 启动时在 stdout 打印带一次性 token 的登录 URL
2. 浏览器访问该 URL，dsh 校验 token 后签发**签名会话 cookie**
3. cookie 绑定交换时的 authority（`PodIP:3080`），后续请求凭 cookie 访问

多用户网关无法让浏览器直连 `PodIP:3080`，因此网关**代浏览器完成交换**：

```
dsh 启动 ──打印 token URL──▶ 网关读 Pod 日志(pods/log 权限)
                              │ GET http://PodIP:3080/?token=…
                              ▼
                         换得会话 cookie(缓存)
浏览器 ──请求(网关 CAS 会话)──▶ 网关 ──注入 dsh cookie,Host=PodIP:3080──▶ dsh
```

- 代理层保持 `Host: PodIP:3080`（httpx 按目标 URL 自动设置），与 cookie 绑定的 authority 一致，链路自洽
- **失效自愈**（已实测）：cookie 会随 dsh 会话密钥失效而死亡 → 代理层收到 401 → 网关重读日志最后一条 token 行（token 可重复用于交换）→ 重新代换 → 重放请求；仍 401 则删 Pod 重建（数据在 PVC 不丢）
- 实测发现 dsh 会把浏览器会话密钥持久化在 `DSH_HOME/.credentials.yaml`（PVC 上），**容器重启后 cookie 依然有效**——自愈链是密钥轮换/异常时的保险

dsh 前端还有一条 **WebSocket 流式通道**（`/api/remote.mux`，承载所有 Remote 流与 `$events` 事件），网关以纯透传桥接（`ws_proxy.py`）：注入同样的 dsh cookie、子协议/查询串原样保留，握手 401 时走与 HTTP 路径同款的两级自愈。

### 两个浏览器侧兼容点（实测踩坑）

- **dsh 拒绝带 `Origin` 的请求**：POST 一律 403，连与其自身 authority 一致的 Origin 也拒。网关→dsh 是集群内服务间调用，代理层剥掉 `origin` / `referer` / `sec-fetch-*` 等浏览器上下文头即可；CSRF 边界由"唯一入口是网关"承担
- **Chrome 的 manifest 拉取不带凭据**（`<link rel="manifest">` 无 crossorigin 属性，登录用户也以未登录面貌到达）：未登录请求 `/manifest.webmanifest` 时网关回退最小 PWA 清单，避免控制台 401 噪音；带会话的请求仍走代理拿真实 manifest

## 快速开始

```bash
cd k8s
./gen-secret.sh                # 生成真实凭据（可选预置 DEEPSEEK_API_KEY）
# 完整部署步骤见 k8s/README.md
```

详见 [k8s/README.md](k8s/README.md)（部署指南、配置参考、排障）。

## dsh 的绑定约束（镜像为何这样写）

以下均为 dsh 原生行为（实测验证），`image/` 的设计全部由此推出：

- **拒绝绑定通配地址**：`dsh --host 0.0.0.0` 启动即报错，必须绑定容器自身 IP → 入口脚本 `dsh-entry` 用 `hostname -i` 取 Pod IP 传给 `--host`；探针用 tcpSocket（kubelet 从节点连 PodIP:3080，与绑定方式匹配）
- **runc WORKDIR 陷阱**：runc 会把镜像里缺失的 WORKDIR 以 root 属主预先创建，空 PVC + `WORKDIR /data/workspace` = node 用户不可写 → 镜像 `WORKDIR /data`，`/data/workspace` 由入口脚本以 node 用户创建
- **npm 原生依赖**：npm 11 需要 `--allow-scripts` 才能安装 node-pty / koffi 的 postinstall 编译产物
- **kubectl port-forward 不可用**：port-forward 连的是 Pod 网络命名空间内的 localhost，而 dsh 只监听 Pod IP —— 这正是"网关按 Pod IP 直连"架构的由来

## 目录

```
image/     dsh 用户镜像（官方 npm 包 + dsh-entry 入口 + dsh-k8s-user-info 身份插件）
gateway/   FastAPI 网关（CAS 认证、K8s 编排、认证代换、HTTP 反代 + WS 桥接）
k8s/       部署清单（kind、RBAC、存储、NetworkPolicy、CAS）+ 部署文档
```

## 已知限制

- **网关单副本**：sandbox_registry.db 是 PVC 上的 SQLite，多副本会脑裂；扩容前需先迁移到外部存储
- **token 是Pod 日志公开信息**：能读 `dsh-system` Pod 日志的集群内身份可换取会话；NetworkPolicy + RBAC（pods/log 仅授权网关 SA）缩小暴露面，生产可进一步收紧
- **模型凭据**：`DEEPSEEK_API_KEY` 可由网关统一预置（Pod env，不落 PVC），留空则用户在 dsh web UI 里自行配置（dsh 原生方式，凭据落自己的 PVC）
- **kind 不执行 NetworkPolicy**：kindnet CNI 不实现，用户 Pod 横向隔离在生产集群（Calico/Cilium）才实际生效
