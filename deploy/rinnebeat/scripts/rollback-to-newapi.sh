#!/bin/sh
set -eu

BASE_DIR=${BASE_DIR:-/home/nekosaki_tsuyuki/services}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SUB2_DIR=${SUB2_DIR:-$(dirname "$SCRIPT_DIR")}
CADDYFILE=${CADDYFILE:-$BASE_DIR/caddy/Caddyfile}
SITE=${SITE:-api.rinnebeat.com}

python3 "$SUB2_DIR/scripts/set-caddy-upstream.py" \
  "$CADDYFILE" "$SITE" "sub2api:8080" "new-api:3000"

container_candidate="/tmp/Caddyfile.rinnebeat.$$"
docker cp "$CADDYFILE" "caddy:$container_candidate"
docker exec caddy caddy validate --config "$container_candidate"
docker exec caddy caddy reload --config "$container_candidate"
docker exec caddy rm -f "$container_candidate"

# 回滚只恢复流量，不停止 Sub2API，便于继续检查数据。
curl --fail --silent --show-error --max-time 10 \
  --resolve "$SITE:443:127.0.0.1" "https://$SITE/api/status" >/dev/null
printf '已回滚 %s 到 NewAPI；CPA 和 NewAPI 未重启。\n' "$SITE"
