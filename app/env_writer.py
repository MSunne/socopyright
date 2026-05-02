"""就地改写 .env：保留注释和行顺序，只替换匹配的 KEY 行，没有则追加到末尾。"""
from __future__ import annotations

import re
from pathlib import Path

_KEY_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _quote(value: str) -> str:
    if value == "" or any(c in value for c in " \t#\"'`$"):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def update_env(path: Path, updates: dict[str, str]) -> None:
    """把 updates 写回 .env；文件不存在则创建。"""
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    new_lines: list[str] = []
    for line in lines:
        m = _KEY_LINE_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            new_lines.append(f"{key}={_quote(remaining.pop(key))}")
        else:
            new_lines.append(line)

    if remaining:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        for k, v in remaining.items():
            new_lines.append(f"{k}={_quote(v)}")

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
