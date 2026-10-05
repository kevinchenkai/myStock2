"""命令行入口：`python -m mystock2 <子命令>`。命令相互独立（NF-08），每次运行输出 run_id 与结果。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mystock2 import __version__
from mystock2.core import db as dbmod
from mystock2.core.config import REPO_ROOT, ConfigError, load_config
from mystock2.core.runs import run_log
from mystock2.instruments.universe import load_universe


def _cmd_version(args) -> int:
    print(__version__)
    return 0


def _cmd_config_show(args) -> int:
    cfg = load_config(args.config)
    print(json.dumps({
        "source": str(cfg.source), "is_example": cfg.is_example, "db_path": str(cfg.db_path),
        "web": {"host": cfg.web.host, "port": cfg.web.port}, "markets": list(cfg.markets),
        "futu": {"host": cfg.futu.host, "port": cfg.futu.port, "trd_env": cfg.futu.trd_env},
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_db_migrate(args) -> int:
    cfg = load_config(args.config)
    applied = dbmod.migrate(cfg.db_path)
    conn = dbmod.connect_writer(cfg.db_path, "core")
    try:
        with run_log(conn, "db migrate", {"db": cfg.db_path.name}) as run:
            run.note(applied=applied, schema_version=max(applied) if applied else None)
            print(f"run_id={run.run_id} applied={applied}")
    finally:
        conn.close()
    return 0


def _cmd_db_status(args) -> int:
    cfg = load_config(args.config)
    print(f"schema_version={dbmod.schema_version(cfg.db_path)}")
    return 0


def _cmd_universe_check(args) -> int:
    path = Path(args.file) if args.file else REPO_ROOT / "config" / "local" / "universe.yaml"
    rep = load_universe(path, known_codes=args.known or ())
    for e in rep.entries:
        flag = "可执行" if e.executable else ("不可执行：" + ",".join(e.blockers) if e.tier == "trade" else "-")
        print(f"{e.code:<12} {e.tier:<6} {e.currency} {flag}")
    for err in rep.errors:
        cand = f"  候选：{', '.join(err['candidates'])}" if err.get("candidates") else ""
        print(f"错误 [{err.get('error')}] {err.get('code')}: {err.get('message')}{cand}", file=sys.stderr)
    return 0 if rep.ok else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mystock2", description="myStock2：账本·透视·教练·记分牌")
    p.add_argument("--config", help="配置文件路径（默认 config.yaml，缺失时回退 config.example.yaml）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="打印版本").set_defaults(fn=_cmd_version)
    sub.add_parser("config-show", help="显示生效配置（不含密钥）").set_defaults(fn=_cmd_config_show)
    d = sub.add_parser("db", help="数据库命令").add_subparsers(dest="dbcmd", required=True)
    d.add_parser("migrate", help="应用迁移").set_defaults(fn=_cmd_db_migrate)
    d.add_parser("status", help="显示 schema 版本").set_defaults(fn=_cmd_db_status)
    u = sub.add_parser("universe", help="标的名单").add_subparsers(dest="ucmd", required=True)
    c = u.add_parser("check", help="校验名单")
    c.add_argument("--file", help="名单文件（默认 config/local/universe.yaml）")
    c.add_argument("--known", nargs="*", help="已知完整代码，用于给裸代码提供候选")
    c.set_defaults(fn=_cmd_universe_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (ConfigError, dbmod.DbError, FileNotFoundError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
