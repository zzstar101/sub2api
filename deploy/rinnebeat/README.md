# Rinnebeat 低资源并行部署

这套配置用于在不停止 NewAPI、CPA、CPAMP 的前提下并行部署 Sub2API。生产域名只有在人工执行切换脚本后才会改变上游。

## 资源边界

- Sub2API：1.25 CPU、1536 MiB、12 个数据库连接、64 个 Redis 连接。
- PostgreSQL：0.40 CPU、768 MiB、40 个服务端连接。
- Redis：0.15 CPU、320 MiB，关闭 AOF，仅用于缓存和调度状态。
- 对外仅监听 `127.0.0.1:18080`；Caddy 通过现有 `services_default` 网络访问。

## 部署顺序

1. 从 `.env.example` 生成权限为 `0600` 的 `.env`，所有密码使用随机值。
2. 执行 `docker compose pull` 和 `docker compose up -d`。
3. 检查 `docker compose ps`、`http://127.0.0.1:18080/health` 和容器资源。
4. 导入账号、用户和 API Key，完成低负载请求测试。
5. 人工执行 `scripts/switch-to-sub2api.sh`。

切换脚本会先检查 Sub2API 健康状态，再备份 Caddyfile、精确替换 `api.rinnebeat.com` 的唯一上游、校验配置并热加载。任何中间步骤失败都会自动恢复 Caddyfile。

NewAPI 用户和有效 Key 可以先只读预演：

```sh
./scripts/migrate-newapi-users.py \
  --newapi-db /home/nekosaki_tsuyuki/services/new-api/data/one-api.db
```

确认数量后添加 `--apply`。写入前脚本会自动生成 PostgreSQL 压缩备份；NewAPI 原始数据库不会被修改。

CPA 账号池也可以先预演：

```sh
./scripts/migrate-cpa-accounts.py \
  --auth-dir /home/nekosaki_tsuyuki/services/cpa/auth \
  --cpa-config /home/nekosaki_tsuyuki/services/cpa/config.docker.yaml
```

脚本默认支持 Codex、OpenAI API 上游、Anthropic 和 Grok，明确跳过 OpenCode 与 Kimi。实际导入会按小批次执行，并在写入前再次备份 PostgreSQL。

## 复合分组与回滚

先预览要绑定的迁移账号与 API Key：

```sh
python3 scripts/configure-sub2api-routing.py
```

确认后创建独立的 `Rinnebeat Composite` 分组，绑定账号与密钥，并生成不含凭据的回滚清单：

```sh
python3 scripts/configure-sub2api-routing.py --apply
```

需要撤销分组绑定时，指定上一步输出的清单：

```sh
python3 scripts/configure-sub2api-routing.py \
  --rollback backups/routing/sub2api-routing-before-YYYYMMDDTHHMMSSZ.json
```

脚本不修改 Caddy，也不会停止 NewAPI、CPA 或 CPA Manager Plus。

## 回滚

执行：

```sh
./scripts/rollback-to-newapi.sh
```

回滚仅把 Caddy 热加载回 `new-api:3000`，不会停止或重启 NewAPI、CPA、CPAMP，也不会删除 Sub2API 数据。
