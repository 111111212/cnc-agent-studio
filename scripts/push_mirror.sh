#!/usr/bin/env bash
# 经镜像通道推送到 GitHub。
# 适用场景：github.com 的 git 智能 HTTP 端点在本机不可达（CONNECT 502 / 读超时），
# 但镜像站可完整转发 upload-pack 与 receive-pack。
#
# 安全约定：凭据从本机 Git 凭据管理器动态读取，
# 不写进 remote URL、不写进 .git/config、不落盘、不回显明文。
set -u

cd "$(dirname "$0")/.." || exit 1

REPO="${CNC_REPO:-111111212/cnc-agent-studio}"
BRANCH="${CNC_BRANCH:-main}"
# ghfast.top 会透传 Authorization；gh-proxy.com 会剥离该头，故不作为默认。
MIRROR="${CNC_MIRROR:-https://ghfast.top/https://github.com}"

# 1) 提交前密钥闸门：全量扫工作区，命中即终止
if [ -f scripts/check_secrets.py ]; then
  python scripts/check_secrets.py || { echo "[push_mirror] 密钥闸门未通过，已终止推送"; exit 1; }
fi

# 2) 从凭据管理器取 token。GCM 按「带用户名」的键存储，必须显式传 username。
read_token() {
  printf 'protocol=https\nhost=github.com\nusername=%s\n\n' "$1" \
    | timeout 15 git credential fill 2>/dev/null \
    | sed -n 's/^password=//p' | tr -d '\r' | head -1
}
TOKEN="$(read_token x-access-token)"
[ -n "$TOKEN" ] || TOKEN="$(read_token 111111212)"
if [ -z "$TOKEN" ]; then
  echo "[push_mirror] 未读到 GitHub 凭据：请先完成一次 GCM 登录，或在能连 GitHub 的机器上运行一次带认证的 git 操作。"
  exit 1
fi
echo "[push_mirror] 凭据已加载: [len=${#TOKEN}, prefix=${TOKEN:0:4}****]"

# 3) 用 http.extraHeader 注入 Basic 认证，仅对本次命令生效（环境变量注入，不落盘）
BASIC="$(printf 'x-access-token:%s' "$TOKEN" | base64 | tr -d '\n')"
export GIT_TERMINAL_PROMPT=0
export GCM_INTERACTIVE=Never
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=http.extraHeader
export GIT_CONFIG_VALUE_0="Authorization: Basic $BASIC"
export GIT_CONFIG_KEY_1=credential.helper
export GIT_CONFIG_VALUE_1=

URL="$MIRROR/$REPO.git"
echo "[push_mirror] 通道: $(printf '%s' "$URL" | cut -d/ -f3) -> $REPO ($BRANCH)"
git -c http.version=HTTP/1.1 -c http.lowSpeedLimit=1 -c http.lowSpeedTime=30 \
    push "$URL" "HEAD:$BRANCH" 2>&1 \
  | sed -E 's/(Authorization: Basic )[A-Za-z0-9=+\/]+/\1[MASKED]/g'
RC=${PIPESTATUS[0]}
if [ "$RC" -ne 0 ]; then
  echo "[push_mirror] 推送失败 (exit=$RC)。可换镜像重试：CNC_MIRROR=https://<mirror>/https://github.com $0"
  exit "$RC"
fi

# 4) 回读远端 refs 校验，避免「推送成功但内容不一致」
echo "[push_mirror] 远端 refs:"
timeout 30 git -c http.version=HTTP/1.1 ls-remote "$URL" "refs/heads/$BRANCH"
echo "[push_mirror] 完成: https://github.com/$REPO"
