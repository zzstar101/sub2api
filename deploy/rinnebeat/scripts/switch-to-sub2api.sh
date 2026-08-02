#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR=${BASE_DIR:-/home/nekosaki_tsuyuki/services}
SUB2_DIR=${SUB2_DIR:-$BASE_DIR/sub2api}
CADDYFILE=${CADDYFILE:-$BASE_DIR/caddy/Caddyfile}
BACKUP_DIR=${BACKUP_DIR:-$SUB2_DIR/backups/caddy}
SITE=${SITE:-api.rinnebeat.com}

# 先验证旁路实例，任何失败都不修改生产路由。
test "$(docker inspect -f '{{.State.Health.Status}}' sub2api-canary)" = "healthy"
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:18080/health >/dev/null

mkdir -p "$BACKUP_DIR"
backup="$BACKUP_DIR/Caddyfile.$(date -u +%Y%m%dT%H%M%SZ)"
cp "$CADDYFILE" "$backup"

rollback() {
  cp "$backup" "$CADDYFILE"
  docker exec caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
}
trap rollback HUP INT TERM ERR

python3 "$SUB2_DIR/scripts/set-caddy-upstream.py" \
  "$CADDYFILE" "$SITE" "new-api:3000" "sub2api:8080"
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
curl --fail --silent --show-error --max-time 10 \
  --resolve "$SITE:443:127.0.0.1" "https://$SITE/health" >/dev/null

trap - HUP INT TERM ERR
printf '已切换 %s 到 Sub2API；回滚备份：%s\n' "$SITE" "$backup"
