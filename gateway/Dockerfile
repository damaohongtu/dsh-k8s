# dsh-gateway:CAS 鉴权 + 每用户 dsh Pod 编排 + 反向代理
#
# 不含任何前端资源 —— web UI 由用户自己的 dsh Pod 原生提供
# (dsh --profile web),网关只做认证、编排与透传。

ARG REGISTRY=docker.1ms.run
FROM ${REGISTRY}/python:3.12-slim

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    --trusted-host pypi.tuna.tsinghua.edu.cn

COPY app/ ./app/

# /data 挂网关 PVC(sandbox registry SQLite)
RUN mkdir -p /data/dshgw
ENV DSHGW_SANDBOX_MODE=k8s

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
