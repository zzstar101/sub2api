#!/usr/bin/env python3
"""把 CPA OAuth 文件和 API 上游转换为 Sub2API 账号。"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml


SUPPORTED_PROVIDERS = {"codex", "openai", "anthropic", "grok"}
SKIPPED_COMPAT_NAMES = {"opencode go", "opencode free"}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def model_mapping(models: object) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(models, list):
        return result
    for item in models:
        if isinstance(item, str):
            name = item.strip()
            alias = name
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            alias = str(item.get("alias") or name).strip()
        else:
            continue
        if name and alias:
            result[alias] = name
    return result


def account_name(prefix: str, source: str, index: int | None = None) -> str:
    value = " ".join(part for part in (prefix, source.strip()) if part).strip()
    if index is not None:
        value = f"{value} #{index}"
    return value[:100]


def oauth_credentials(record: dict[str, object], provider: str) -> dict[str, object]:
    allowed = {
        "access_token",
        "refresh_token",
        "id_token",
        "email",
        "sub",
        "token_type",
        "scope",
        "base_url",
        "token_endpoint",
        "client_id",
        "team_id",
    }
    credentials = {key: record[key] for key in allowed if record.get(key) not in (None, "")}
    expires_at = record.get("expires_at") or record.get("expired")
    if expires_at:
        credentials["expires_at"] = expires_at
    if provider == "codex" and record.get("account_id"):
        credentials["chatgpt_account_id"] = record["account_id"]
    return credentials


def data_account(
    *,
    name: str,
    platform: str,
    account_type: str,
    credentials: dict[str, object],
    source: str,
    concurrency: int,
    priority: int,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    account_extra = {"rinnebeat_migration_source": source}
    if extra:
        account_extra.update(extra)
    return {
        "name": name,
        "notes": f"Migrated from CPA: {source}",
        "platform": platform,
        "type": account_type,
        "credentials": credentials,
        "extra": account_extra,
        "concurrency": concurrency,
        "priority": priority,
    }


def load_oauth_accounts(auth_dir: Path) -> tuple[list[dict[str, object]], Counter[str]]:
    accounts: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    for path in sorted(auth_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            counts["invalid"] += 1
            continue
        provider = str(record.get("type") or "").strip().lower()
        if record.get("disabled", False):
            counts[f"disabled_{provider or 'unknown'}"] += 1
            continue
        if provider not in {"codex", "xai"}:
            counts[f"skipped_{provider or 'unknown'}"] += 1
            continue
        target_provider = "grok" if provider == "xai" else "codex"
        platform = "grok" if provider == "xai" else "openai"
        credentials = oauth_credentials(record, provider)
        if not credentials.get("access_token") or not credentials.get("refresh_token"):
            counts[f"invalid_{target_provider}"] += 1
            continue
        label = str(record.get("email") or path.stem)
        accounts.append(
            data_account(
                name=account_name("CPA", label),
                platform=platform,
                account_type="oauth",
                credentials=credentials,
                source=path.name,
                concurrency=1,
                priority=max(0, int(record.get("priority") or 10)),
            )
        )
        counts[target_provider] += 1
    return accounts, counts


def api_key_credentials(item: dict[str, object], base_url: str, models: object) -> dict[str, object]:
    api_key = item.get("api-key") or item.get("api_key")
    credentials: dict[str, object] = {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
    }
    mapping = model_mapping(models)
    if mapping:
        credentials["model_mapping"] = mapping
    return credentials


def load_api_accounts(config_path: Path) -> tuple[list[dict[str, object]], Counter[str]]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    accounts: list[dict[str, object]] = []
    counts: Counter[str] = Counter()

    for index, item in enumerate(config.get("codex-api-key") or [], 1):
        if not isinstance(item, dict) or item.get("disabled", False):
            counts["disabled_openai"] += 1
            continue
        base_url = str(item.get("base-url") or "").strip()
        credentials = api_key_credentials(item, base_url, item.get("models"))
        if not base_url or not credentials.get("api_key"):
            counts["invalid_openai"] += 1
            continue
        host = urlparse(base_url).netloc or base_url
        accounts.append(
            data_account(
                name=account_name("CPA OpenAI", host, index),
                platform="openai",
                account_type="apikey",
                credentials=credentials,
                source=f"codex-api-key[{index}]",
                concurrency=3,
                priority=max(0, int(item.get("priority") or 50)),
            )
        )
        counts["openai"] += 1

    for index, item in enumerate(config.get("claude-api-key") or [], 1):
        if not isinstance(item, dict) or item.get("disabled", False):
            counts["disabled_anthropic"] += 1
            continue
        base_url = str(item.get("base-url") or "").strip()
        credentials = api_key_credentials(item, base_url, item.get("models"))
        if not base_url or not credentials.get("api_key"):
            counts["invalid_anthropic"] += 1
            continue
        host = urlparse(base_url).netloc or base_url
        accounts.append(
            data_account(
                name=account_name("CPA Anthropic", host, index),
                platform="anthropic",
                account_type="apikey",
                credentials=credentials,
                source=f"claude-api-key[{index}]",
                concurrency=3,
                priority=max(0, int(item.get("priority") or 50)),
            )
        )
        counts["anthropic"] += 1

    for section_index, section in enumerate(config.get("openai-compatibility") or [], 1):
        if not isinstance(section, dict) or section.get("disabled", False):
            counts["disabled_openai"] += 1
            continue
        section_name = str(section.get("name") or f"compat-{section_index}").strip()
        if section_name.lower() in SKIPPED_COMPAT_NAMES:
            counts["skipped_opencode"] += 1
            continue
        base_url = str(section.get("base-url") or "").strip()
        entries = section.get("api-key-entries") or []
        if isinstance(entries, (str, dict)):
            entries = [entries]
        for entry_index, raw_entry in enumerate(entries, 1):
            entry = {"api-key": raw_entry} if isinstance(raw_entry, str) else raw_entry
            if not isinstance(entry, dict) or entry.get("disabled", False):
                counts["disabled_openai"] += 1
                continue
            credentials = api_key_credentials(entry, base_url, section.get("models"))
            if not base_url or not credentials.get("api_key"):
                counts["invalid_openai"] += 1
                continue
            accounts.append(
                data_account(
                    name=account_name("CPA", section_name, entry_index),
                    platform="openai",
                    account_type="apikey",
                    credentials=credentials,
                    source=f"openai-compatibility[{section_index}].api-key-entries[{entry_index}]",
                    concurrency=3,
                    priority=max(0, int(entry.get("priority") or 50)),
                    # CPA 的 openai-compatibility 渠道按 Chat Completions 协议转发。
                    # 显式覆盖可避免 Sub2API 在能力未知时默认请求 /responses。
                    extra={"openai_responses_mode": "force_chat_completions"},
                )
            )
            counts["openai"] += 1
    return accounts, counts


class Sub2APIClient:
    def __init__(self, base_url: str, email: str, password: str) -> None:
        self.base_url = base_url.rstrip("/") + "/api/v1"
        self.token = ""
        login = self.post("/auth/login", {"email": email, "password": password, "turnstile_token": ""})
        self.token = str(login["access_token"])

    def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        headers = {"Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4())}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            try:
                detail = json.loads(raw)
                code = detail.get("code") or detail.get("message") or f"HTTP {error.code}"
            except json.JSONDecodeError:
                code = f"HTTP {error.code}"
            raise RuntimeError(str(code)) from error
        return body.get("data", body) if isinstance(body, dict) else body


def existing_account_keys(container: str, database: str, user: str) -> set[tuple[str, str]]:
    output = subprocess.check_output(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            user,
            "-d",
            database,
            "-At",
            "-c",
            "select platform || E'\\t' || name from accounts where deleted_at is null",
        ]
    )
    result: set[tuple[str, str]] = set()
    for line in output.decode("utf-8").splitlines():
        platform, separator, name = line.partition("\t")
        if separator:
            result.add((platform, name))
    return result


def backup_database(container: str, database: str, user: str, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"sub2api-before-cpa-accounts-{timestamp}.sql.gz"
    process = subprocess.Popen(
        ["docker", "exec", container, "pg_dump", "-U", user, "-d", database, "--no-owner"],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    with gzip.open(target, "wb") as output:
        while chunk := process.stdout.read(1024 * 1024):
            output.write(chunk)
    if process.wait() != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError("PostgreSQL 备份失败")
    os.chmod(target, 0o600)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-dir", type=Path, required=True)
    parser.add_argument("--cpa-config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--providers", default=",".join(sorted(SUPPORTED_PROVIDERS)))
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--batch-delay", type=float, default=2.0)
    parser.add_argument("--postgres-container", default="sub2api-postgres")
    parser.add_argument("--postgres-database", default="sub2api")
    parser.add_argument("--postgres-user", default="sub2api")
    parser.add_argument("--backup-dir", type=Path, default=Path("backups/postgres"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    selected = {item.strip().lower() for item in args.providers.split(",") if item.strip()}
    unknown = selected - SUPPORTED_PROVIDERS
    if unknown:
        parser.error("未知 provider：" + ", ".join(sorted(unknown)))
    if args.batch_size <= 0 or args.batch_delay < 0:
        parser.error("批次大小必须大于 0，批次间隔不能为负数")

    oauth_accounts, oauth_counts = load_oauth_accounts(args.auth_dir)
    api_accounts, api_counts = load_api_accounts(args.cpa_config)
    all_accounts = oauth_accounts + api_accounts
    accounts = [
        account
        for account in all_accounts
        if (
            "codex"
            if account["platform"] == "openai" and account["type"] == "oauth"
            else account["platform"]
        )
        in selected
    ]
    counts = oauth_counts + api_counts
    print("预检：" + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print(f"本次选择 {len(accounts)} 个账号，providers={','.join(sorted(selected))}")
    if not args.apply:
        print("当前为只读预演；添加 --apply 才会写入旁路 Sub2API。")
        return 0

    env = load_env(args.env_file)
    backup = backup_database(
        args.postgres_container,
        args.postgres_database,
        args.postgres_user,
        args.backup_dir,
    )
    existing = existing_account_keys(args.postgres_container, args.postgres_database, args.postgres_user)
    pending = [
        account
        for account in accounts
        if (str(account["platform"]), str(account["name"])) not in existing
    ]
    print(f"已存在 {len(accounts) - len(pending)} 个，本次待导入 {len(pending)} 个。")
    client = Sub2APIClient(args.base_url, env["ADMIN_EMAIL"], env["ADMIN_PASSWORD"])

    created = 0
    failed = 0
    for offset in range(0, len(pending), args.batch_size):
        batch = pending[offset : offset + args.batch_size]
        result = client.post(
            "/admin/accounts/data",
            {
                "data": {
                    "type": "sub2api-data",
                    "version": 1,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "proxies": [],
                    "accounts": batch,
                },
                "skip_default_group_bind": False,
            },
        )
        batch_created = int(result.get("account_created") or 0)
        batch_failed = int(result.get("account_failed") or 0)
        created += batch_created
        failed += batch_failed
        print(
            f"批次 {offset // args.batch_size + 1}：created={batch_created} failed={batch_failed}"
        )
        if offset + args.batch_size < len(pending):
            time.sleep(args.batch_delay)

    print(f"导入完成：created={created} failed={failed}；迁移前备份：{backup}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
