#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

assert_contains() {
    local file="$1"
    local expected="$2"
    if ! grep -Fq -- "${expected}" "${file}"; then
        echo "FAIL: ${file} 缺少: ${expected}" >&2
        exit 1
    fi
}

assert_not_contains() {
    local file="$1"
    local unexpected="$2"
    if grep -Fq -- "${unexpected}" "${file}"; then
        echo "FAIL: ${file} 仍包含: ${unexpected}" >&2
        exit 1
    fi
}

assert_equal() {
    local actual="$1"
    local expected="$2"
    if [[ "${actual}" != "${expected}" ]]; then
        echo "FAIL: 期望 '${expected}'，实际 '${actual}'" >&2
        exit 1
    fi
}

SCHEDULER="${PROJECT_ROOT}/scripts/start_daily_scheduler.sh"
CRONTAB="${PROJECT_ROOT}/config/daily.crontab"
COMPOSE="${PROJECT_ROOT}/docker/docker-compose.yml"
COMPOSE_BUILD="${PROJECT_ROOT}/docker/docker-compose-build.yml"
ENTRYPOINT="${PROJECT_ROOT}/docker/entrypoint.sh"
DOCKER_ENV_EXAMPLE="${PROJECT_ROOT}/docker/.env.example"
GITIGNORE="${PROJECT_ROOT}/.gitignore"
CONFIG="${PROJECT_ROOT}/config/config.yaml"

bash -n "${SCHEDULER}"
bash -n "${ENTRYPOINT}"
# shellcheck source=/dev/null
source "${ENTRYPOINT}"
DEFAULT_CRONS='0 10 * * *;30 10 * * 1;0,30 11 * * 1;0 12 * * 1'
assert_equal "$(CRON_SCHEDULES= CRON_SCHEDULE='5 6 * * *' resolve_cron_list)" '5 6 * * *'
assert_equal "$(CRON_SCHEDULES='7 8 * * *' CRON_SCHEDULE='5 6 * * *' resolve_cron_list)" '7 8 * * *'
assert_equal "$(CRON_SCHEDULES= CRON_SCHEDULE= resolve_cron_list)" "${DEFAULT_CRONS}"
assert_not_contains "${SCHEDULER}" "/share/"
assert_not_contains "${CRONTAB}" "/share/"
assert_contains "${SCHEDULER}" 'BASH_SOURCE[0]'
assert_contains "${SCHEDULER}" 'command -v supercronic'
assert_contains "${SCHEDULER}" 'command -v uv'
assert_contains "${CRONTAB}" '"$PROJECT_DIR"'
assert_contains "${CRONTAB}" '"$UV_BIN"'
assert_contains "${CRONTAB}" '0 10 * * *'
assert_contains "${CRONTAB}" '每天静默采集，周一生成上一自然周 PDF；周一 10:30—12:00 为气象周报重试'
assert_contains "${SCHEDULER}" '每天 10:00'
assert_contains "${COMPOSE}" 'dockerfile: docker/Dockerfile'
assert_contains "${COMPOSE}" 'CRON_SCHEDULES=${CRON_SCHEDULES:-}'
assert_contains "${COMPOSE}" 'CRON_SCHEDULE=${CRON_SCHEDULE:-}'
assert_contains "${COMPOSE_BUILD}" 'CRON_SCHEDULES=${CRON_SCHEDULES:-}'
assert_contains "${COMPOSE_BUILD}" 'CRON_SCHEDULE=${CRON_SCHEDULE:-}'
assert_contains "${ENTRYPOINT}" 'CRON_LIST="$(resolve_cron_list)"'
assert_contains "${ENTRYPOINT}" "IFS=';' read -ra EXPRESSIONS"
assert_not_contains "${COMPOSE}" 'image: wantcat/trendradar:latest'
assert_contains "${COMPOSE}" 'HTTP_PROXY: ${DOCKER_PROXY_URL:-http://host.docker.internal:7892}'
assert_contains "${COMPOSE}" 'HTTPS_PROXY: ${DOCKER_PROXY_URL:-http://host.docker.internal:7892}'
assert_contains "${COMPOSE}" 'http_proxy: ${DOCKER_PROXY_URL:-http://host.docker.internal:7892}'
assert_contains "${COMPOSE}" 'https_proxy: ${DOCKER_PROXY_URL:-http://host.docker.internal:7892}'
assert_contains "${COMPOSE}" 'HTTP_PROXY=${DOCKER_PROXY_URL:-http://host.docker.internal:7892}'
assert_contains "${COMPOSE}" 'HTTPS_PROXY=${DOCKER_PROXY_URL:-http://host.docker.internal:7892}'
assert_contains "${COMPOSE}" 'http_proxy=${DOCKER_PROXY_URL:-http://host.docker.internal:7892}'
assert_contains "${COMPOSE}" 'https_proxy=${DOCKER_PROXY_URL:-http://host.docker.internal:7892}'
assert_contains "${COMPOSE}" 'NEWS_PROXY_URL=${DOCKER_PROXY_URL:-http://host.docker.internal:7892}'
assert_contains "${COMPOSE}" 'NO_PROXY=${DOCKER_NO_PROXY:-apigw.hnaicc.cn,qyapi.weixin.qq.com}'
assert_contains "${COMPOSE}" 'no_proxy=${DOCKER_NO_PROXY:-apigw.hnaicc.cn,qyapi.weixin.qq.com}'
assert_contains "${COMPOSE}" '"host.docker.internal:host-gateway"'
assert_contains "${GITIGNORE}" 'docker/.env'
assert_contains "${DOCKER_ENV_EXAMPLE}" 'DOCKER_PROXY_URL=http://host.docker.internal:7892'
assert_contains "${DOCKER_ENV_EXAMPLE}" 'DOCKER_NO_PROXY=apigw.hnaicc.cn,qyapi.weixin.qq.com'
assert_contains "${DOCKER_ENV_EXAMPLE}" 'CRON_SCHEDULES="0 10 * * *;30 10 * * 1;0,30 11 * * 1;0 12 * * 1"'
assert_contains "${DOCKER_ENV_EXAMPLE}" 'IMMEDIATE_RUN=false'
assert_contains "${DOCKER_ENV_EXAMPLE}" 'WEWORK_WEBHOOK_URL='
assert_contains "${PROJECT_ROOT}/docker/Dockerfile" 'chromium fonts-noto-cjk poppler-utils'
assert_contains "${DOCKER_ENV_EXAMPLE}" 'AI_API_KEY='
assert_contains "${CONFIG}" '  api_key: ""'
assert_contains "${CONFIG}" '  model: "openai//data/minimax-2.5-fp8"'
assert_contains "${CONFIG}" '  fallback_models: ["openai//models/DeepSeek-R1-G2-static"]'

if grep -Eq '^(WEWORK_WEBHOOK_URL|AI_API_KEY|EMAIL_PASSWORD|S3_SECRET_ACCESS_KEY)=.+' \
    "${DOCKER_ENV_EXAMPLE}"; then
    echo "FAIL: ${DOCKER_ENV_EXAMPLE} 包含非空敏感值" >&2
    exit 1
fi

if git -C "${PROJECT_ROOT}" ls-files --error-unmatch docker/.env >/dev/null 2>&1; then
    echo "FAIL: docker/.env 仍被 Git 跟踪" >&2
    exit 1
fi

if [[ -f "${PROJECT_ROOT}/docker/.env" ]]; then
    ai_model="$(
        sed -n 's/^AI_MODEL=//p' "${PROJECT_ROOT}/docker/.env" | tail -n 1
    )"
    ai_model="${ai_model#\"}"
    ai_model="${ai_model%\"}"
    if [[ -n "${ai_model}" && ! "${ai_model}" =~ ^[[:alnum:]_.-]+/.+ ]]; then
        echo "FAIL: docker/.env 的 AI_MODEL 缺少 LiteLLM Provider 前缀" >&2
        exit 1
    fi
fi

echo "PASS: 本地部署路径可移植性检查通过"
