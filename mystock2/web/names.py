"""标的中文名：给视图结果里的代码补 `code_name`（展示用参考数据，来自 `instrument_name`；Web 只读）。

- 视图结果里凡是带字符串型 `code`、且形如 US./HK. 完整代码的 dict，补一个 `code_name`；
- 结果顶层再给一份 `code_names`（{代码: 中文名}，只含结果里出现过的代码），供前端给下拉、文字里的代码补名称；
- 查不到名称就不加、不报错（表不存在、旧库未迁移同样不报错）：名称只是展示，不是视图的数据来源；
- 名称不是密封内容（不涉及任何 AI 单信息），也不参与任何排序/筛选/计算——这些仍只用代码。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from mystock2.instruments.code_map import HK_RE, US_RE

CHUNK = 400


def is_code(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 12 and bool(HK_RE.match(value) or US_RE.match(value))


def collect_codes(data: Any) -> set[str]:
    """结果里出现过的完整代码（任意位置的整串字符串）。"""
    found: set[str] = set()
    stack = [data]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, (list, tuple)):
            stack.extend(x)
        elif is_code(x):
            found.add(x)
    return found


def lookup(conn: sqlite3.Connection, codes: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    ordered = sorted(codes)
    try:
        for i in range(0, len(ordered), CHUNK):
            part = ordered[i:i + CHUNK]
            for r in conn.execute(f"SELECT code, name FROM instrument_name WHERE code IN ({','.join('?' * len(part))})", part):
                if r["name"]:
                    out[r["code"]] = r["name"]
    except sqlite3.Error:                    # 表不存在（旧库）等：没有名称而已
        return {}
    return out


def annotate(conn: sqlite3.Connection, data: dict) -> dict:
    """原地给 data 补 `code_name` 与顶层 `code_names`；一次查询。"""
    names = lookup(conn, collect_codes(data))
    if not names:
        return data
    stack: list[Any] = [data]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            c = x.get("code")
            if is_code(c) and c in names and "code_name" not in x:
                x["code_name"] = names[c]
            stack.extend(x.values())
        elif isinstance(x, (list, tuple)):
            stack.extend(x)
    data.setdefault("code_names", names)
    return data
