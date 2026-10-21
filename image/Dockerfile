# dsh 用户镜像:DeepSeek Harness (dsh) 官方 npm 包容器化
#
# dsh 没有官方容器镜像(官方文档只给 npm 安装方式),本镜像按官方部署说明打包:
# - Node 版本要求 ^22.19.0 || >=24.0.0(官方 package.json engines)
# - npm 11 默认拦截原生依赖的安装脚本,需要 --allow-scripts 显式放行
#   (node-pty/koffi 等会触发 node-gyp 编译,构建阶段带 python3/make/g++)
# - dsh web 拒绝绑定通配地址(0.0.0.0 启动即报错),入口脚本 dsh-entry
#   用 hostname -i 取容器 IP 传给 --host,网关按 Pod IP 直连寻址
#
# 运行时布局(PVC 整目录挂载,与 openclaw-k8s / pi-k8s 对齐):
#   /data            → PVC 挂载点(HOME)
#   /data/.dsh       → dsh 状态(DSH_HOME:profiles 配置、.credentials.yaml、会话)
#   /data/workspace  → 默认工作区
#
# 注意 WORKDIR 是 /data 而不是 /data/workspace:runc 会在容器进程启动前以
# root 身份创建缺失的 cwd,PVC 首次挂载为空时会把 workspace 建成 root 属主
# (node 用户不可写)。目录骨架由 dsh-entry 以 node 用户创建,属主正确。
#
# 构建参数(国内网络可覆盖镜像源):
#   --build-arg REGISTRY=docker.1ms.run
#   --build-arg NPM_REGISTRY=https://registry.npmmirror.com
#   --build-arg DSH_VERSION=0.2.1-rc.2

ARG REGISTRY=docker.1ms.run
ARG NODE_VERSION=24
ARG DSH_VERSION=0.2.1-rc.2

# ---- 构建阶段:原生依赖可能触发 node-gyp 编译 ----
FROM ${REGISTRY}/node:${NODE_VERSION} AS build
ARG DSH_VERSION
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g --registry="${NPM_REGISTRY}" \
      --allow-scripts=@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs \
      "@deepseek-ai/dsh@${DSH_VERSION}"

# ---- 运行阶段 ----
FROM ${REGISTRY}/node:${NODE_VERSION}-slim
ARG DSH_VERSION

# agent 会在容器内执行 shell 命令,预装常用工具
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git openssh-client ripgrep jq unzip xz-utils less procps \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js /usr/local/bin/dsh \
    && dsh --version

# 平台身份插件:放 /opt(镜像不触碰 dsh 安装树,dsh 本体零修改),
# 由 dsh-entry 复制到 DSH_HOME/node_modules 并以 --patch 启用,
# 经 DSH_K8S_* 环境取值
COPY dsh-k8s-user-info /opt/dsh-k8s-user-info

# 官方 node 镜像自带 uid/gid 1000 的 node 用户,与 Pod securityContext 对齐
COPY --chmod=0755 dsh-entry /usr/local/bin/dsh-entry

ENV HOME=/data \
    DSH_HOME=/data/.dsh \
    DSH_WORKSPACE=/data/workspace \
    TERM=xterm-256color

USER node
WORKDIR /data

EXPOSE 3080

ENTRYPOINT ["dsh-entry"]
