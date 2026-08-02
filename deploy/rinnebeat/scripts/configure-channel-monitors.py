#!/usr/bin/env python3
"""为 Rinnebeat Sub2API 业务分组配置低频端到端渠道监控。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ROUTING_SCRIPT = SCRIPT_DIR / "configure-sub2api-routing.py"
MONITOR_KEY_PREFIX = "channel-monitor:"
MONITOR_NAME_PREFIX = "Rinnebeat / "
DEFAULT_EXCLUDED_GROUPS = {"default"}

MODEL_PREFERENCES = {
    "Rinnebeat Composite": ["gpt-5.5", "gpt-5.6-sol", "deepseek-v4-flash"],
    "Claude": ["claude-haiku-4-5-20251001", "claude-fable-5", "claude-opus-4-8"],
    "DeepSeek": ["deepseek-v4-flash", "deepseek-v4-pro"],
    "Mimo": ["mimo-v2.5", "mimo-v2.5-pro"],
    "MiniMax": ["minimax-m3", "minimax-m2.7", "minimax-m2.5"],
    "MoonShot": ["kimi-k2.6", "kimi-k2.7-code", "kimi-k2.7-code-highspeed"],
    "OpenAI": ["gpt-5.5", "gpt-5.6-sol", "gpt-5.4"],
    "xAI": ["grok-4.5"],
    "智谱": ["glm-5.2"],
    "通义千问": ["qwen3.5-397b-a17b", "qwen3.5-plus"],
}


def load_routing_module():
    spec = importlib.util.spec_from_file_location("rinnebeat_routing", ROUTING_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载共享脚本: {ROUTING_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def list_user_keys(client, routing) -> list[dict[str, object]]:
    return routing.paginated_items(client, "/keys")


def list_monitors(client, routing) -> list[dict[str, object]]:
    return routing.paginated_items(client, "/admin/channel-monitors")


def visible_models(endpoint: str, api_key: str) -> list[str]:
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/models",
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "rinnebeat-channel-monitor-setup/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"读取分组模型失败: HTTP {error.code}: {detail}") from error
    data = body.get("data", []) if isinstance(body, dict) else []
    return sorted(
        {
            str(item["id"])
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    )


def choose_model(group_name: str, models: list[str]) -> str:
    available = set(models)
    for model in MODEL_PREFERENCES.get(group_name, []):
        if model in available:
            return model
    for model in models:
        lowered = model.lower()
        if not any(token in lowered for token in ("image", "video", "audio", "tts")):
            return model
    if MODEL_PREFERENCES.get(group_name):
        return MODEL_PREFERENCES[group_name][0]
    raise RuntimeError(f"分组 {group_name} 没有适合文本探测的可用模型")


def ensure_key(client, keys: list[dict[str, object]], group: dict[str, object]) -> tuple[dict[str, object], bool]:
    name = MONITOR_KEY_PREFIX + str(group["name"])
    existing = next((item for item in keys if item.get("name") == name), None)
    if existing is not None:
        if int(existing.get("group_id") or 0) != int(group["id"]):
            existing = client.put(f"/keys/{int(existing['id'])}", {"group_id": int(group["id"])})
        return existing, False
    created = client.post(
        "/keys",
        {
            "name": name,
            "group_id": int(group["id"]),
        },
    )
    if not isinstance(created, dict) or not created.get("key"):
        raise RuntimeError(f"创建分组 {group['name']} 的监控 Key 后返回异常")
    keys.append(created)
    return created, True


def monitor_payload(
    group_name: str,
    endpoint: str,
    api_key: str,
    model: str,
    interval_seconds: int,
    jitter_seconds: int,
) -> dict[str, object]:
    provider = "anthropic" if group_name == "Claude" else "openai"
    return {
        "name": MONITOR_NAME_PREFIX + group_name,
        "provider": provider,
        "api_mode": "chat_completions",
        "endpoint": endpoint.rstrip("/"),
        "api_key": api_key,
        "primary_model": model,
        "extra_models": [],
        "group_name": group_name,
        "enabled": True,
        "interval_seconds": interval_seconds,
        "jitter_seconds": jitter_seconds,
        "extra_headers": {},
        "body_override_mode": "off",
    }


def ensure_monitor(client, monitors: list[dict[str, object]], payload: dict[str, object]) -> tuple[dict[str, object], str]:
    name = str(payload["name"])
    existing = next((item for item in monitors if item.get("name") == name), None)
    if existing is None:
        created = client.post("/admin/channel-monitors", payload)
        if not isinstance(created, dict):
            raise RuntimeError(f"创建监控 {name} 后返回异常")
        monitors.append(created)
        return created, "created"
    comparable_fields = (
        "name",
        "provider",
        "api_mode",
        "endpoint",
        "primary_model",
        "extra_models",
        "group_name",
        "enabled",
        "interval_seconds",
        "jitter_seconds",
        "extra_headers",
        "body_override_mode",
    )
    if all(existing.get(field) == payload.get(field) for field in comparable_fields):
        return existing, "unchanged"
    updated = client.put(f"/admin/channel-monitors/{int(existing['id'])}", payload)
    if not isinstance(updated, dict):
        raise RuntimeError(f"更新监控 {name} 后返回异常")
    return updated, "updated"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=SCRIPT_DIR.parent / ".env")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--endpoint", default="https://api.rinnebeat.com")
    parser.add_argument("--interval-seconds", type=int, default=3600)
    parser.add_argument("--jitter-seconds", type=int, default=1200)
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 15 <= args.interval_seconds <= 3600:
        raise SystemExit("--interval-seconds 必须在 15 到 3600 之间")
    if not 0 <= args.jitter_seconds <= 3585:
        raise SystemExit("--jitter-seconds 必须在 0 到 3585 之间")

    routing = load_routing_module()
    env = routing.load_env(args.env_file)
    client = routing.Sub2APIClient(args.base_url, env["ADMIN_EMAIL"], env["ADMIN_PASSWORD"])
    groups = [
        group
        for group in routing.all_groups(client)
        if group.get("status") == "active" and str(group.get("name")) not in DEFAULT_EXCLUDED_GROUPS
    ]
    print(f"发现业务分组 {len(groups)} 个，排除空系统分组: {', '.join(sorted(DEFAULT_EXCLUDED_GROUPS))}")
    if not args.apply:
        for group in groups:
            print(f"  预演: {group['name']}")
        return 0

    keys = list_user_keys(client, routing)
    monitors = list_monitors(client, routing)
    results: list[tuple[str, str, str]] = []
    for index, group in enumerate(groups):
        group_name = str(group["name"])
        key, key_created = ensure_key(client, keys, group)
        models = visible_models(args.endpoint, str(key["key"]))
        model = choose_model(group_name, models)
        monitor, monitor_status = ensure_monitor(
            client,
            monitors,
            monitor_payload(
                group_name,
                args.endpoint,
                str(key["key"]),
                model,
                args.interval_seconds,
                args.jitter_seconds,
            ),
        )
        key_status = "new-key" if key_created else "existing-key"
        results.append((group_name, model, f"{monitor_status},{key_status}"))
        print(f"  {group_name}: model={model} monitor={monitor_status} key={key_status}")

        if args.run_once:
            check = client.post(f"/admin/channel-monitors/{int(monitor['id'])}/run", {})
            check_items = check.get("results", []) if isinstance(check, dict) else []
            primary = check_items[0] if check_items else {}
            print(
                f"    check={primary.get('status', 'unknown')} "
                f"latency_ms={primary.get('latency_ms', 'n/a')}"
            )
            if index + 1 < len(groups):
                time.sleep(2)

    print(f"完成: 监控 {len(results)} 个，周期 {args.interval_seconds}s，抖动 ±{args.jitter_seconds}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
