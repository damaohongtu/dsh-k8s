#!/usr/bin/env bash
# 生成 k8s/30-secret.yaml（含真实凭据，已被 .gitignore 排除）
#
# 用法：cd k8s && ./gen-secret.sh
# 模型凭据（DEEPSEEK_API_KEY）可选：留空则用户在 dsh web UI 自行配置。
set -euo pipefail
cd "$(dirname "$0")"

SESSION_SECRET_KEY=$(openssl rand -hex 32)
DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"

cat > 30-secret.yaml <<EOF
# 由 gen-secret.sh 生成 $(date '+%Y-%m-%d %H:%M:%S')，勿提交 git
apiVersion: v1
kind: Secret
metadata:
  name: dsh-secrets
  namespace: dsh-system
type: Opaque
stringData:
  DSHGW_SESSION_SECRET_KEY: "${SESSION_SECRET_KEY}"
  DSHGW_DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}"
EOF

chmod 600 30-secret.yaml
echo "已生成 30-secret.yaml"
[ -n "${DEEPSEEK_API_KEY}" ] || echo "提示：DEEPSEEK_API_KEY 为空，用户需在 dsh web UI 自行配置模型"
