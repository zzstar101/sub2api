#!/usr/bin/env python3
"""按 NewAPI 的分组、倍率和模型可见性配置 Sub2API。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType


COMPOSITE_GROUPS = {"OpenAI", "Claude"}
GROK_GROUPS = {"xAI"}
OPENAI_PREFIXES = ("deepseek-", "glm-", "hy3-", "kimi-", "mimo-", "minimax-", "qwen")


def load_routing_module() -> ModuleType:
    path = Path(__file__).with_name("configure-sub2api-routing.py")
    spec = importlib.util.spec_from_file_location("rinnebeat_sub2api_routing", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载路由辅助模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_key(value: str) -> str:
    value = value.strip()
    return value if value.startswith("sk-") else "sk-" + value


def load_newapi_configuration(
    db_path: Path,
    newapi_url: str,
) -> tuple[dict[str, float], dict[str, list[str]], dict[str, str]]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    options = dict(
        connection.execute(
            "select key, value from options where key in ('GroupRatio', 'UserUsableGroups')"
        ).fetchall()
    )
    ratios = {
        str(name): float(value)
        for name, value in json.loads(options.get("GroupRatio") or "{}").items()
    }
    usable_groups = {
        str(name): str(label)
        for name, label in json.loads(options.get("UserUsableGroups") or "{}").items()
    }

    token_rows = connection.execute(
        """
        select t.key, coalesce(nullif(t.`group`, ''), nullif(u.`group`, ''), 'default')
        from tokens t
        join users u on u.id = t.user_id
        where t.deleted_at is null and t.status = 1
        order by t.id
        """
    ).fetchall()
    token_groups = {normalize_key(str(key)): str(group) for key, group in token_rows}
    sample_keys: dict[str, str] = {}
    for key, group in token_rows:
        sample_keys.setdefault(str(group), normalize_key(str(key)))

    models_by_group: dict[str, list[str]] = {}
    for group, key in sample_keys.items():
        request = urllib.request.Request(
            newapi_url.rstrip("/") + "/v1/models",
            headers={"Authorization": "Bearer " + key},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"读取 NewAPI 分组 {group} 的模型失败: HTTP {error.code}") from error
        models_by_group[group] = sorted(
            {str(item["id"]) for item in body.get("data", []) if item.get("id")}
        )
    connection.close()

    all_known_models = sorted({model for models in models_by_group.values() for model in models})
    for group in usable_groups:
        if group in models_by_group:
            continue
        if group == "通义千问":
            models_by_group[group] = [model for model in all_known_models if model.startswith("qwen")]
        elif group == "MiniMax":
            models_by_group[group] = [model for model in all_known_models if model.startswith("minimax-")]
        else:
            models_by_group[group] = []
    for group in usable_groups:
        ratios.setdefault(group, 1.0)
    return ratios, models_by_group, token_groups


def platform_for_group(name: str) -> str:
    if name in COMPOSITE_GROUPS:
        return "composite"
    if name in GROK_GROUPS:
        return "grok"
    return "openai"


def ensure_group(
    client: object,
    routing: ModuleType,
    name: str,
    rate_multiplier: float,
    models: list[str],
) -> dict[str, object]:
    platform = platform_for_group(name)
    payload: dict[str, object] = {
        "name": name,
        "description": "Migrated from NewAPI group",
        "platform": platform,
        "rate_multiplier": rate_multiplier,
        "subscription_type": "standard",
        "models_list_config": {"enabled": True, "models": models},
    }
    existing = routing.find_group(client, name)
    if existing is None:
        result = client.post("/admin/groups", payload)
    else:
        if existing.get("platform") != platform:
            raise RuntimeError(
                f"Sub2API 同名分组平台不匹配: {name}={existing.get('platform')}, expected={platform}"
            )
        result = client.put(
            f"/admin/groups/{int(existing['id'])}",
            {
                "rate_multiplier": rate_multiplier,
                "status": "active",
                "models_list_config": payload["models_list_config"],
            },
        )
    if not isinstance(result, dict):
        raise RuntimeError(f"配置 Sub2API 分组失败: {name}")
    if platform == "composite":
        routing.ensure_alias_routes(client, int(result["id"]))
    return result


def collect_api_keys(
    client: object,
    routing: ModuleType,
    group_ids: set[int],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for group_id in sorted(group_ids):
        for item in routing.group_keys(client, group_id):
            key = str(item.get("key") or "")
            if key:
                result[key] = item
    return result


def write_manifest(
    backup_dir: Path,
    accounts: list[dict[str, object]],
    api_keys: list[dict[str, object]],
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"sub2api-before-newapi-groups-{stamp}.json"
    document = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
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
                "id": int(item["id"]),
                "name": str(item.get("name") or ""),
                "group_id": int(item["group_id"]) if item.get("group_id") else 0,
            }
            for item in api_keys
        ],
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def bind_accounts(
    client: object,
    accounts: list[dict[str, object]],
    groups: dict[str, dict[str, object]],
) -> None:
    composite_ids = {int(groups[name]["id"]) for name in COMPOSITE_GROUPS if name in groups}
    openai_ids = {
        int(group["id"])
        for name, group in groups.items()
        if platform_for_group(name) == "openai"
    }
    grok_ids = {
        int(group["id"])
        for name, group in groups.items()
        if platform_for_group(name) == "grok"
    }
    batches: dict[tuple[int, ...], list[int]] = defaultdict(list)
    for account in accounts:
        desired = {int(value) for value in (account.get("group_ids") or [])}
        desired.update(composite_ids)
        platform = str(account.get("platform") or "")
        if platform == "openai":
            desired.update(openai_ids)
        elif platform == "grok":
            desired.update(grok_ids)
        batches[tuple(sorted(desired))].append(int(account["id"]))

    for group_ids, account_ids in batches.items():
        result = client.post(
            "/admin/accounts/bulk-update",
            {
                "account_ids": account_ids,
                "group_ids": list(group_ids),
                "confirm_mixed_channel_risk": True,
            },
        )
        if not isinstance(result, dict) or int(result.get("failed") or 0) != 0:
            raise RuntimeError(f"账号分组绑定失败: {result}")


def apply_groups(
    client: object,
    routing: ModuleType,
    ratios: dict[str, float],
    models_by_group: dict[str, list[str]],
    token_groups: dict[str, str],
    fallback_group_id: int,
    backup_dir: Path,
) -> None:
    groups = {
        name: ensure_group(client, routing, name, ratios[name], models_by_group.get(name, []))
        for name in sorted(ratios)
        if name != "default"
    }
    candidate_group_ids = {fallback_group_id, *(int(group["id"]) for group in groups.values())}
    target_keys = collect_api_keys(client, routing, candidate_group_ids)
    missing = sorted(set(token_groups) - set(target_keys))
    if missing:
        raise RuntimeError(f"有 {len(missing)} 个 NewAPI Key 未在 Sub2API 中找到")

    accounts = routing.paginated_items(client, "/admin/accounts")
    selected_keys = [target_keys[key] for key in token_groups]
    manifest = write_manifest(backup_dir, accounts, selected_keys)
    bind_accounts(client, accounts, groups)

    for key, group_name in token_groups.items():
        if group_name not in groups:
            raise RuntimeError(f"NewAPI Key 使用了未配置分组: {group_name}")
        client.put(
            f"/admin/api-keys/{int(target_keys[key]['id'])}",
            {"group_id": int(groups[group_name]["id"])},
        )

    counts = {
        name: len(routing.group_keys(client, int(group["id"])))
        for name, group in groups.items()
    }
    expected: dict[str, int] = defaultdict(int)
    for group_name in token_groups.values():
        expected[group_name] += 1
    if any(counts.get(name, 0) != count for name, count in expected.items()):
        raise RuntimeError(f"API Key 分组数量校验失败: actual={counts}, expected={dict(expected)}")
    print("分组迁移完成：" + ", ".join(f"{name}={counts[name]}" for name in sorted(counts)))
    print(f"回滚清单: {manifest}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--newapi-db", type=Path, required=True)
    parser.add_argument("--newapi-url", default="http://127.0.0.1:3000")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--fallback-group-id", type=int, default=2)
    parser.add_argument("--backup-dir", type=Path, default=Path("backups/routing"))
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    routing = load_routing_module()
    ratios, models_by_group, token_groups = load_newapi_configuration(
        args.newapi_db,
        args.newapi_url,
    )
    print("NewAPI 分组：")
    for name in sorted(ratios):
        print(
            f"  {name}: platform={platform_for_group(name)} "
            f"ratio={ratios[name]:g} models={len(models_by_group.get(name, []))} "
            f"keys={sum(group == name for group in token_groups.values())}"
        )
    if not args.apply:
        print("dry-run 完成；使用 --apply 执行")
        return 0

    env = routing.load_env(args.env_file)
    client = routing.Sub2APIClient(args.base_url, env["ADMIN_EMAIL"], env["ADMIN_PASSWORD"])
    apply_groups(
        client,
        routing,
        ratios,
        models_by_group,
        token_groups,
        args.fallback_group_id,
        args.backup_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
