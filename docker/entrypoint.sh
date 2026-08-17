#!/bin/bash
set -e

DEFAULT_CRON_SCHEDULES='0 10 * * 0,2-6;10,30 12 * * 1;0,30 13 * * 1'

resolve_cron_list() {
    if [ -n "${CRON_SCHEDULES:-}" ]; then
        echo "$CRON_SCHEDULES"
    elif [ -n "${CRON_SCHEDULE:-}" ]; then
        echo "$CRON_SCHEDULE"
    else
        echo "$DEFAULT_CRON_SCHEDULES"
    fi
}

# 允许 shell 测试只加载纯函数，不执行容器启动流程。
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
fi

# 检查配置文件
if [ ! -f "/app/config/config.yaml" ] || [ ! -f "/app/config/frequency_words.txt" ]; then
    echo "❌ 配置文件缺失"
    exit 1
fi

case "${RUN_MODE:-cron}" in
"once")
    echo "🔄 单次执行"
    exec python -m trendradar
    ;;
"cron")
    # 兼容旧 CRON_SCHEDULE，并支持分号分隔的多条短触发。
    CRON_LIST="$(resolve_cron_list)"
    : > /tmp/crontab
    IFS=';' read -ra EXPRESSIONS <<< "$CRON_LIST"
    for CRON_EXPR in "${EXPRESSIONS[@]}"; do
        CRON_EXPR="$(echo "$CRON_EXPR" | xargs)"
        if ! echo "$CRON_EXPR" | grep -qE '^[0-9*/,[:space:]-]+$'; then
            echo "❌ CRON_SCHEDULES 格式非法: $CRON_EXPR"
            exit 1
        fi
        echo "$CRON_EXPR cd /app && python -m trendradar" >> /tmp/crontab
    done
    
    echo "📅 生成的crontab内容:"
    cat /tmp/crontab

    if ! /usr/local/bin/supercronic -test /tmp/crontab; then
        echo "❌ crontab格式验证失败"
        exit 1
    fi

    # 立即执行一次（如果配置了）
    if [ "${IMMEDIATE_RUN:-false}" = "true" ]; then
        echo "▶️ 立即执行一次"
        python -m trendradar
    fi

    # 启动 Web 服务器
    echo "🌐 启动 Web 服务器..."
    python manage.py start_webserver

    echo "⏰ 启动supercronic: $CRON_LIST"
    echo "🎯 supercronic 将作为 PID 1 运行"

    exec /usr/local/bin/supercronic -passthrough-logs /tmp/crontab
    ;;
*)
    exec "$@"
    ;;
esac
