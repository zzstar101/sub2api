#!/usr/bin/env python3
"""把 NewAPI 用户和有效 API Key 迁移到旁路 Sub2API。"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class MigratedUser:
    source_id: int
    email: str
    password_hash: str
    role: str
    balance: float
    status: str
    username: str
    created_at: int


@dataclass(frozen=True)
class MigratedKey:
    source_id: int
    user_source_id: int
    key: str
    name: str
    status: str
    created_at: int
    last_used_at: int
    quota: float
    quota_used: float
    expires_at: int
    ip_whitelist: list[str]


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def normalize_email(source_id: int, email: str | None, username: str | None) -> str:
    candidate = (email or "").strip().lower()
    if EMAIL_PATTERN.match(candidate):
        return candidate
    local = re.sub(r"[^a-z0-9._-]+", "-", (username or "").strip().lower()).strip("-.")
    if not local:
        local = f"user-{source_id}"
    return f"newapi-{local}-{source_id}@migrated.invalid"


def normalize_key(value: str) -> str:
    value = value.strip()
    return value if value.startswith("sk-") else "sk-" + value


def normalize_ips(raw: str | None) -> list[str]:
    value = (raw or "").strip()
    if not value or value == "*":
        return []
    try:
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return [str(item).strip() for item in decoded if str(item).strip() not in ("", "*")]
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in re.split(r"[,\n\r]+", value) if item.strip() not in ("", "*")]


def load_source(db_path: Path, quota_per_unit: float) -> tuple[list[MigratedUser], list[MigratedKey]]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    users: list[MigratedUser] = []
    keys: list[MigratedKey] = []

    for row in connection.execute(
        """
        SELECT id, email, username, password, role, quota, status, created_at
        FROM users
        WHERE deleted_at IS NULL
        ORDER BY id
        """
    ):
        users.append(
            MigratedUser(
                source_id=int(row["id"]),
                email=normalize_email(int(row["id"]), row["email"], row["username"]),
                password_hash=str(row["password"] or ""),
                role="admin" if int(row["role"] or 0) >= 10 else "user",
                balance=float(row["quota"] or 0) / quota_per_unit,
                status="active" if int(row["status"] or 0) == 1 else "inactive",
                username=str(row["username"] or "")[:100],
                created_at=max(0, int(row["created_at"] or 0)),
            )
        )

    for row in connection.execute(
        """
        SELECT id, user_id, key, name, status, created_time, accessed_time,
               expired_time, remain_quota, unlimited_quota, used_quota, allow_ips
        FROM tokens
        WHERE deleted_at IS NULL AND status = 1
        ORDER BY id
        """
    ):
        unlimited = bool(row["unlimited_quota"])
        used = float(row["used_quota"] or 0) / quota_per_unit
        remaining = float(row["remain_quota"] or 0) / quota_per_unit
        keys.append(
            MigratedKey(
                source_id=int(row["id"]),
                user_source_id=int(row["user_id"]),
                key=normalize_key(str(row["key"] or "")),
                name=(str(row["name"] or "") or f"NewAPI Key {row['id']}")[:100],
                status="active",
                created_at=max(0, int(row["created_time"] or 0)),
                last_used_at=max(0, int(row["accessed_time"] or 0)),
                quota=0 if unlimited else max(0, remaining + used),
                quota_used=0 if unlimited else max(0, used),
                expires_at=max(0, int(row["expired_time"] or 0)),
                ip_whitelist=normalize_ips(row["allow_ips"]),
            )
        )
    connection.close()
    return users, keys


def build_sql(users: list[MigratedUser], keys: list[MigratedKey]) -> str:
    user_rows = []
    for user in users:
        user_rows.append(
            "(" + ",".join(
                [
                    str(user.source_id),
                    sql_string(user.email),
                    sql_string(user.password_hash),
                    sql_string(user.role),
                    f"{user.balance:.8f}",
                    sql_string(user.status),
                    sql_string(user.username),
                    str(user.created_at),
                ]
            ) + ")"
        )

    key_rows = []
    for item in keys:
        key_rows.append(
            "(" + ",".join(
                [
                    str(item.source_id),
                    str(item.user_source_id),
                    sql_string(item.key),
                    sql_string(item.name),
                    sql_string(item.status),
                    str(item.created_at),
                    str(item.last_used_at),
                    f"{item.quota:.8f}",
                    f"{item.quota_used:.8f}",
                    str(item.expires_at),
                    sql_string(json.dumps(item.ip_whitelist, separators=(",", ":"))),
                ]
            ) + ")"
        )

    user_values = ",\n".join(user_rows)
    key_values = ",\n".join(key_rows)
    return f"""
BEGIN;
CREATE TEMP TABLE newapi_users (
    source_id bigint PRIMARY KEY,
    email text NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL,
    balance numeric NOT NULL,
    status text NOT NULL,
    username text NOT NULL,
    created_at_epoch bigint NOT NULL
) ON COMMIT DROP;
INSERT INTO newapi_users VALUES
{user_values};

INSERT INTO users (email, password_hash, role, balance, concurrency, status, username, notes, created_at, updated_at)
SELECT source.email, source.password_hash, source.role, source.balance, 5, source.status,
       source.username, 'Migrated from NewAPI',
       CASE WHEN source.created_at_epoch > 0 THEN to_timestamp(source.created_at_epoch) ELSE now() END,
       now()
FROM newapi_users source
WHERE NOT EXISTS (
    SELECT 1 FROM users existing
    WHERE lower(existing.email) = lower(source.email) AND existing.deleted_at IS NULL
);

CREATE TEMP TABLE newapi_user_map ON COMMIT DROP AS
SELECT source.source_id, target.id AS target_id
FROM newapi_users source
JOIN users target ON lower(target.email) = lower(source.email) AND target.deleted_at IS NULL;

CREATE TEMP TABLE newapi_keys (
    source_id bigint PRIMARY KEY,
    user_source_id bigint NOT NULL,
    key text NOT NULL,
    name text NOT NULL,
    status text NOT NULL,
    created_at_epoch bigint NOT NULL,
    last_used_at_epoch bigint NOT NULL,
    quota numeric NOT NULL,
    quota_used numeric NOT NULL,
    expires_at_epoch bigint NOT NULL,
    ip_whitelist jsonb NOT NULL
) ON COMMIT DROP;
INSERT INTO newapi_keys VALUES
{key_values};

INSERT INTO api_keys (
    user_id, key, name, group_id, status, created_at, updated_at, last_used_at,
    quota, quota_used, expires_at, ip_whitelist, ip_blacklist
)
SELECT user_map.target_id, source.key, source.name,
       (SELECT id FROM groups WHERE name = 'default' AND deleted_at IS NULL ORDER BY id LIMIT 1),
       source.status,
       CASE WHEN source.created_at_epoch > 0 THEN to_timestamp(source.created_at_epoch) ELSE now() END,
       now(),
       CASE WHEN source.last_used_at_epoch > 0 THEN to_timestamp(source.last_used_at_epoch) ELSE NULL END,
       source.quota, source.quota_used,
       CASE WHEN source.expires_at_epoch > 0 THEN to_timestamp(source.expires_at_epoch) ELSE NULL END,
       source.ip_whitelist, '[]'::jsonb
FROM newapi_keys source
JOIN newapi_user_map user_map ON user_map.source_id = source.user_source_id
WHERE NOT EXISTS (
    SELECT 1 FROM api_keys existing
    WHERE existing.key = source.key AND existing.deleted_at IS NULL
);
COMMIT;
"""


def backup_database(container: str, database: str, user: str, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"sub2api-before-newapi-users-{timestamp}.sql.gz"
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


def apply_sql(container: str, database: str, user: str, sql: str) -> None:
    completed = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", database],
        input=sql.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("NewAPI 用户迁移事务失败，数据库已自动回滚")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--newapi-db", type=Path, required=True)
    parser.add_argument("--postgres-container", default="sub2api-postgres")
    parser.add_argument("--postgres-database", default="sub2api")
    parser.add_argument("--postgres-user", default="sub2api")
    parser.add_argument("--quota-per-unit", type=float, default=500000.0)
    parser.add_argument("--backup-dir", type=Path, default=Path("backups/postgres"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.quota_per_unit <= 0:
        parser.error("--quota-per-unit 必须大于 0")
    users, keys = load_source(args.newapi_db, args.quota_per_unit)
    print(f"预检完成：用户 {len(users)}，有效 Key {len(keys)}，配额单位 {args.quota_per_unit:g}")
    if not args.apply:
        print("当前为只读预演；添加 --apply 才会写入旁路 Sub2API。")
        return 0

    backup = backup_database(
        args.postgres_container,
        args.postgres_database,
        args.postgres_user,
        args.backup_dir,
    )
    apply_sql(
        args.postgres_container,
        args.postgres_database,
        args.postgres_user,
        build_sql(users, keys),
    )
    print(f"迁移完成；迁移前备份保存在 {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
