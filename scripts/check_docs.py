#!/usr/bin/env python3
"""文档检查（参考 V1 同名脚本，按 V2 精简）：本地 Markdown 链接存在、docs 子目录文件命名合规。

用法：python scripts/check_docs.py   （退出码非 0 表示有问题）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(?:_[a-z0-9]+)*_(codex|claude|gpt|grok|cursor|human|unknown)(_r\d+)?_\d{8}\.(md|html)$")
CATEGORIES = {"prd", "plans", "records", "research", "guides", "protocols"}
FIXED = {"README.md", "COLLABORATION.md", "OPEN_ITEMS.md", "PROJECT.md"}
USER_ORIGINALS = {"V2项目 idea.md"}          # 用户亲笔材料保持原名
ADR_RE = re.compile(r"^\d{4}-[a-z0-9-]+\.md$")
SKIP_DIRS = {".git", "node_modules", "data", "backups", "exports", ".pytest_cache", ".ruff_cache"}


def md_files():
    for p in ROOT.rglob("*.md"):
        if not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and not any(s.endswith(".egg-info") for s in p.relative_to(ROOT).parts):
            yield p


def check_links() -> list[str]:
    problems = []
    for f in md_files():
        text = f.read_text(encoding="utf-8")
        for m in LINK_RE.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path = (f.parent / target.replace("%20", " ")).resolve()
            if not path.exists():
                problems.append(f"断链：{f.relative_to(ROOT)} -> {target}")
    return problems


def check_names() -> list[str]:
    problems = []
    docs = ROOT / "docs"
    for f in docs.rglob("*"):
        if not f.is_file() or f.name == ".DS_Store":
            continue
        rel = f.relative_to(docs)
        if len(rel.parts) == 1:
            if rel.name not in FIXED and rel.name not in USER_ORIGINALS:
                problems.append(f"docs 根目录出现非固定文件：{rel}")
            continue
        cat = rel.parts[0]
        if cat == "adr":
            if not ADR_RE.match(f.name):
                problems.append(f"ADR 命名应为 NNNN-标题.md：{rel}")
        elif cat in CATEGORIES:
            if f.name in {"README.md", "PROVENANCE.md"}:
                continue
            if not NAME_RE.match(f.name):
                problems.append(f"文档命名应为 主题_作者_[rN_]YYYYMMDD.ext：{rel}")
        else:
            problems.append(f"未知文档目录：{rel}")
    return problems


def main() -> int:
    problems = check_links() + check_names()
    for p in problems:
        print(p)
    print(f"check_docs: {'发现 %d 个问题' % len(problems) if problems else 'OK'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
