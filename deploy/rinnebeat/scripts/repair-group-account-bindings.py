#!/usr/bin/env python3
"""按账号 model_mapping 修复 NewAPI 分组的上游账号绑定。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
MANAGED_GROUP_NAMES = {
    "Claude",
    "DeepSeek",
    "Mimo",
    "MiniMax",
    "MoonShot",
    "OpenAI",
    "xAI",
    "智谱",
    "通义千问",
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def group_models(group: dict[str, object]) -> list[str]:
    config = group.get("models_list_config") or {}
    if not isinstance(config, dict):
        return []
    models = config.get("models") or []
    return [str(model) for model in models if str(model).strip()]


def write_manifest(backup_dir: Path, accounts: list[dict[str, object]]) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"sub2api-before-group-binding-repair-{stamp}.json"
    document = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "accounts": [
            {
                "id": int(account["id"]),
                "group_ids": sorted(int(value) for value in (account.get("group_ids") or [])),
            }
            for account in accounts
        ],
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=SCRIPT_DIR.parent / ".env")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--backup-dir", type=Path, default=SCRIPT_DIR.parent / "backups" / "routing")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    routing = load_module("rinnebeat_routing", SCRIPT_DIR / "configure-sub2api-routing.py")
    migration = load_module("rinnebeat_group_migration", SCRIPT_DIR / "migrate-newapi-groups.py")
    env = routing.load_env(args.env_file)
    client = routing.Sub2APIClient(args.base_url, env["ADMIN_EMAIL"], env["ADMIN_PASSWORD"])
    groups = {
        str(group["name"]): group
        for group in routing.all_groups(client)
        if str(group.get("name")) in MANAGED_GROUP_NAMES and group.get("status") == "active"
    }
    accounts = routing.paginated_items(client, "/admin/accounts")
    models_by_group = {name: group_models(group) for name, group in groups.items()}

    print(f"活动分组 {len(groups)} 个，活动账号 {len(accounts)} 个")
    for name in sorted(groups):
        print(f"  {name}: models={len(models_by_group[name])}")
    if not args.apply:
        return 0

    manifest = write_manifest(args.backup_dir, accounts)
    migration.bind_accounts(client, accounts, groups, models_by_group)
    refreshed = routing.paginated_items(client, "/admin/accounts")
    counts = {
        name: sum(int(group["id"]) in (account.get("group_ids") or []) for account in refreshed)
        for name, group in groups.items()
    }
    for name in sorted(counts):
        print(f"  {name}: accounts={counts[name]}")
    print(f"回滚清单: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
