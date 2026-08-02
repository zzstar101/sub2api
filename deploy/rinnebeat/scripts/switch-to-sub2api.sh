#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR=${BASE_DIR:-/home/nekosaki_tsuyuki/services}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SUB2_DIR=${SUB2_DIR:-$(dirname "$SCRIPT_DIR")}
CADDYFILE=${CADDYFILE:-$BASE_DIR/caddy/Caddyfile}
BACKUP_DIR=${BACKUP_DIR:-$SUB2_DIR/backups/caddy}
SITE=${SITE:-api.rinnebeat.com}

# 先验证旁路实例，任何失败都不修改生产路由。
test "$(docker inspect -f '{{.State.Health.Status}}' sub2api-canary)" = "healthy"
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:18080/health >/dev/null

mkdir -p "$BACKUP_DIR"
backup="$BACKUP_DIR/Caddyfile.$(date -u +%Y%m%dT%H%M%SZ)"
cp "$CADDYFILE" "$backup"

sync_caddyfile() {
  container_candidate="/tmp/Caddyfile.rinnebeat.$$"
  docker cp "$CADDYFILE" "caddy:$container_candidate"
  docker exec caddy caddy validate --config "$container_candidate"
  docker exec caddy sh -c "cat '$container_candidate' > /etc/caddy/Caddyfile && rm -f '$container_candidate'"
  docker exec caddy caddy reload --config /etc/caddy/Caddyfile
}

rollback() {
  trap - HUP INT TERM ERR
  cp "$backup" "$CADDYFILE"
  sync_caddyfile >/dev/null 2>&1 || true
}
trap rollback HUP INT TERM ERR

python3 "$SUB2_DIR/scripts/set-caddy-upstream.py" \
  "$CADDYFILE" "$SITE" "new-api:3000" "sub2api:8080"
sync_caddyfile
curl --fail --silent --show-error --max-time 10 \
  --resolve "$SITE:443:127.0.0.1" "https://$SITE/health" >/dev/null

trap - HUP INT TERM ERR
printf '已切换 %s 到 Sub2API；回滚备份：%s\n' "$SITE" "$backup"
