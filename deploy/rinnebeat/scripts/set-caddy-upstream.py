#!/usr/bin/env python3
"""只修改指定 Caddy 站点块中的唯一上游。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def replace_upstream(text: str, site: str, old: str, new: str) -> str:
    lines = text.splitlines(keepends=True)
    depth = 0
    in_site = False
    replacements = 0

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not in_site and stripped == f"{site} {{":
            in_site = True
            depth = 1
            continue

        if not in_site:
            continue

        if stripped == f"reverse_proxy {old}":
            indent = line[: len(line) - len(line.lstrip())]
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            lines[index] = f"{indent}reverse_proxy {new}{newline}"
            replacements += 1

        depth += line.count("{") - line.count("}")
        if depth == 0:
            in_site = False
            break

    if replacements != 1:
        raise RuntimeError(
            f"站点 {site!r} 中预期替换 1 个 {old!r}，实际找到 {replacements} 个"
        )
    return "".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("site")
    parser.add_argument("old")
    parser.add_argument("new")
    args = parser.parse_args()

    source = args.path.read_text(encoding="utf-8")
    result = replace_upstream(source, args.site, args.old, args.new)
    temporary = args.path.with_suffix(args.path.suffix + ".tmp")
    temporary.write_text(result, encoding="utf-8", newline="")
    # Docker bind mount 会持有原 inode；覆盖内容而不是 rename，避免容器继续读取旧文件。
    shutil.copyfile(temporary, args.path)
    temporary.unlink()


if __name__ == "__main__":
    main()
