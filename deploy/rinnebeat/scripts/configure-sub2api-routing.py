#!/usr/bin/env python3
"""为迁移自 NewAPI/CPA 的数据配置可回滚的 Sub2API 复合分组。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "glm-5",
    "glm-5.1",
    "glm-5.2",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-image-1.5",
    "gpt-image-2",
    "grok-3-mini",
    "grok-3-mini-fast",
    "grok-4.20-0309-non-reasoning",
    "grok-4.20-0309-reasoning",
    "grok-4.20-multi-agent-0309",
    "grok-4.3",
    "grok-4.5",
    "grok-build-0.1",
    "grok-composer-2.5-fast",
    "grok-imagine-image",
    "grok-imagine-image-quality",
    "grok-imagine-video",
    "grok-imagine-video-1.5-preview",
    "hy3-preview",
    "kimi-k2",
    "kimi-k2-thinking",
    "kimi-k2.5",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "kimi-k2.7-code-highspeed",
    "kimi-k3",
    "kimi-k3-256k",
    "mimo-v2-omni",
    "mimo-v2-pro",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "minimax-m2.5",
    "minimax-m2.7",
    "minimax-m3",
    "qwen3.5-397b-a17b",
    "qwen3.5-plus",
    "qwen3.6-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
]

# 标准 gpt-/claude-/grok- 前缀由 Sub2API 内置检测处理。
OPENAI_ALIAS_PREFIXES = ["deepseek-", "glm-", "hy3-", "kimi-", "mimo-", "minimax-", "qwen"]


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


class Sub2APIClient:
    def __init__(self, base_url: str, email: str, password: str) -> None:
        self.base_url = base_url.rstrip("/") + "/api/v1"
        self.token = ""
        login = self.post(
            "/auth/login",
            {"email": email, "password": password, "turnstile_token": ""},
            authenticated=False,
        )
        self.token = str(login["access_token"])

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        authenticated: bool = True,
    ) -> object:
        headers = {"Accept": "application/json", "User-Agent": "rinnebeat-sub2api-routing/1"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = str(uuid.uuid4())
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if authenticated and self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            try:
                detail = json.loads(raw)
                message = detail.get("message") or detail.get("error") or detail.get("code")
            except json.JSONDecodeError:
                message = raw[:300]
            raise RuntimeError(f"{method} {path}: HTTP {error.code}: {message}") from error
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    def get(self, path: str) -> object:
        return self.request("GET", path)

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        authenticated: bool = True,
    ) -> object:
        return self.request("POST", path, payload, authenticated=authenticated)

    def put(self, path: str, payload: dict[str, object]) -> object:
        return self.request("PUT", path, payload)


def paginated_items(client: Sub2APIClient, path: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        data = client.get(f"{path}{separator}page={page}&page_size=100")
        if not isinstance(data, dict):
            raise RuntimeError(f"分页接口返回格式异常: {path}")
        batch = data.get("items") or []
        items.extend(item for item in batch if isinstance(item, dict))
        if page >= int(data.get("pages") or 1):
            return items
        page += 1


def all_groups(client: Sub2APIClient) -> list[dict[str, object]]:
    data = client.get("/admin/groups/all?include_inactive=true")
    if not isinstance(data, list):
        raise RuntimeError("分组列表返回格式异常")
    return [item for item in data if isinstance(item, dict)]


def find_group(client: Sub2APIClient, name: str) -> dict[str, object] | None:
    return next((group for group in all_groups(client) if group.get("name") == name), None)


def migrated_accounts(client: Sub2APIClient) -> list[dict[str, object]]:
    accounts = paginated_items(client, "/admin/accounts")
    return [
        account
        for account in accounts
        if str(account.get("notes") or "").startswith("Migrated from CPA:")
    ]


def group_keys(client: Sub2APIClient, group_id: int) -> list[dict[str, object]]:
    return paginated_items(client, f"/admin/groups/{group_id}/api-keys")


def ensure_composite_group(
    client: Sub2APIClient,
    group_name: str,
    models: list[str],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": group_name,
        "description": "Rollback-safe NewAPI + CPA migration canary",
        "platform": "composite",
        "rate_multiplier": 1,
        "subscription_type": "standard",
        "models_list_config": {"enabled": True, "models": models},
    }
    group = find_group(client, group_name)
    if group is None:
        created = client.post("/admin/groups", payload)
        if not isinstance(created, dict):
            raise RuntimeError("创建复合分组后返回格式异常")
        return created
    if group.get("platform") != "composite":
        raise RuntimeError(f"同名分组不是 composite: {group_name}")
    updated = client.put(
        f"/admin/groups/{int(group['id'])}",
        {"models_list_config": payload["models_list_config"], "status": "active"},
    )
    if not isinstance(updated, dict):
        raise RuntimeError("更新复合分组后返回格式异常")
    return updated


def ensure_alias_routes(client: Sub2APIClient, group_id: int) -> None:
    raw_routes = client.get(f"/admin/groups/{group_id}/composite-routes")
    existing = {
        (str(route.get("public_model")), str(route.get("match_type")), str(route.get("endpoint"))): route
        for route in raw_routes if isinstance(route, dict)
    } if isinstance(raw_routes, list) else {}
    for priority, prefix in enumerate(OPENAI_ALIAS_PREFIXES, 10):
        payload: dict[str, object] = {
            "public_model": prefix,
            "match_type": "prefix",
            "target_platform": "openai",
            "upstream_model": "",
            "endpoint": "any",
            "priority": priority,
            "enabled": True,
            "notes": "Migrated CPA alias; preserve requested model",
        }
        current = existing.get((prefix, "prefix", "any"))
        if current is None:
            client.post(f"/admin/groups/{group_id}/composite-routes", payload)
        else:
            client.put(
                f"/admin/groups/{group_id}/composite-routes/{int(current['id'])}",
                payload,
            )


def write_manifest(
    backup_dir: Path,
    group: dict[str, object],
    accounts: list[dict[str, object]],
    keys: list[dict[str, object]],
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"sub2api-routing-before-{stamp}.json"
    document = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_group": {"id": int(group["id"]), "name": str(group["name"])},
        "accounts": [
            {
                "id": int(account["id"]),
                "name": str(account.get("name") or ""),
                "group_ids": [int(value) for value in (account.get("group_ids") or [])],
            }
            for account in accounts
        ],
        "api_keys": [
            {
                "id": int(key["id"]),
                "name": str(key.get("name") or ""),
                "group_id": int(key["group_id"]) if key.get("group_id") else 0,
            }
            for key in keys
        ],
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def apply_routing(
    client: Sub2APIClient,
    group_name: str,
    source_group_id: int,
    backup_dir: Path,
    models: list[str],
) -> None:
    accounts = migrated_accounts(client)
    if not accounts:
        raise RuntimeError("没有找到迁移自 CPA 的账号")

    existing_group = find_group(client, group_name)
    candidate_group_ids = [source_group_id]
    if existing_group is not None:
        candidate_group_ids.append(int(existing_group["id"]))
    keys_by_id: dict[int, dict[str, object]] = {}
    for group_id in candidate_group_ids:
        for key in group_keys(client, group_id):
            keys_by_id[int(key["id"])] = key
    keys = list(keys_by_id.values())
    if not keys:
        raise RuntimeError("源分组和目标分组都没有找到待迁移 API Key")

    group = ensure_composite_group(client, group_name, models)
    group_id = int(group["id"])
    ensure_alias_routes(client, group_id)
    manifest = write_manifest(backup_dir, group, accounts, keys)

    account_ids = [int(account["id"]) for account in accounts]
    result = client.post(
        "/admin/accounts/bulk-update",
        {
            "account_ids": account_ids,
            "group_ids": [group_id],
            "confirm_mixed_channel_risk": True,
        },
    )
    if not isinstance(result, dict) or int(result.get("failed") or 0) != 0:
        raise RuntimeError(f"账号分组绑定未全部成功: {result}")

    for key in keys:
        client.put(f"/admin/api-keys/{int(key['id'])}", {"group_id": group_id})

    bound_accounts = migrated_accounts(client)
    bound_count = sum(group_id in (account.get("group_ids") or []) for account in bound_accounts)
    bound_keys = group_keys(client, group_id)
    if bound_count != len(accounts) or len(bound_keys) != len(keys):
        raise RuntimeError(
            f"绑定后校验失败: accounts={bound_count}/{len(accounts)}, keys={len(bound_keys)}/{len(keys)}"
        )
    print(f"复合分组: {group_name} (id={group_id})")
    print(f"账号绑定: {bound_count}")
    print(f"API Key 绑定: {len(bound_keys)}")
    print(f"模型列表: {len(models)}")
    print(f"回滚清单: {manifest}")


def rollback_routing(client: Sub2APIClient, manifest_path: Path) -> None:
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    accounts = document.get("accounts") or []
    grouped: dict[tuple[int, ...], list[int]] = {}
    for account in accounts:
        group_ids = tuple(int(value) for value in account.get("group_ids") or [])
        grouped.setdefault(group_ids, []).append(int(account["id"]))
    for group_ids, account_ids in grouped.items():
        result = client.post(
            "/admin/accounts/bulk-update",
            {
                "account_ids": account_ids,
                "group_ids": list(group_ids),
                "confirm_mixed_channel_risk": True,
            },
        )
        if not isinstance(result, dict) or int(result.get("failed") or 0) != 0:
            raise RuntimeError(f"账号回滚未全部成功: {result}")
    for key in document.get("api_keys") or []:
        client.put(f"/admin/api-keys/{int(key['id'])}", {"group_id": int(key.get("group_id") or 0)})
    print(f"已按清单回滚账号 {len(accounts)} 个、API Key {len(document.get('api_keys') or [])} 个")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--group-name", default="Rinnebeat Composite")
    parser.add_argument("--source-group-id", type=int, default=1)
    parser.add_argument("--backup-dir", type=Path, default=Path("backups/routing"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env = load_env(args.env_file)
    email = env.get("ADMIN_EMAIL") or os.environ.get("ADMIN_EMAIL")
    password = env.get("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD")
    if not email or not password:
        print("缺少 ADMIN_EMAIL 或 ADMIN_PASSWORD", file=sys.stderr)
        return 2
    client = Sub2APIClient(args.base_url, email, password)
    if args.rollback:
        rollback_routing(client, args.rollback)
        return 0

    accounts = migrated_accounts(client)
    source_keys = group_keys(client, args.source_group_id)
    group = find_group(client, args.group_name)
    target_keys = group_keys(client, int(group["id"])) if group else []
    print(f"计划账号: {len(accounts)}")
    print(f"计划 API Key: {len({int(key['id']) for key in source_keys + target_keys})}")
    print(f"目标分组: {args.group_name} ({'已存在' if group else '待创建'})")
    print(f"模型列表: {len(DEFAULT_MODELS)}")
    if not args.apply:
        print("dry-run 完成；使用 --apply 执行")
        return 0
    apply_routing(
        client,
        args.group_name,
        args.source_group_id,
        args.backup_dir,
        DEFAULT_MODELS,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
