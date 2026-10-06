"""Flask 应用工厂：只读 Web（实施方案 §3.2、WP3.1）。

- 只通过 `core.db.connect_ro` 打开数据库（`mode=ro`），**不使用任何写连接**；
- 统一路由 `GET /api/v/<view_id>`：返回 `{status, header（新鲜度）, data, ...}`；
- 只允许回环地址监听，且校验 Host 头（防 DNS 重绑定）；
- **唯一的 POST**：`POST /api/review/deal`（复盘卡「AI 评价」的请求/刷新）。它**不写库、不调模型**，只校验后拉起受控 CLI
  `mystock2 review deal`（独立进程，持有 `review` 写权限，把评价写进缓存表）；Web 进程本身仍只有只读连接；
- 库不存在/未迁移/个别数据缺失都返回业务状态（status=unavailable），页面显示原因，不崩溃；
- 前端是原生 JS 静态文件，无 CDN；严格的 CSP 禁止外部资源与内联脚本。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_file
from flask.json.provider import DefaultJSONProvider

from mystock2.core import db as dbmod
from mystock2.core.config import REPO_ROOT, Config, ConfigError, is_loopback
from mystock2.core.money import to_db
from mystock2.core.timeutil import iso_utc, utc_now
from mystock2.replay import review_cache
from mystock2.web import names, registry
from mystock2.web.common import ViewUnavailable, build_header

log = logging.getLogger("mystock2.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_VIEWS_CONFIG = REPO_ROOT / "config" / "views.yaml"
DEFAULT_UNIVERSE = REPO_ROOT / "config" / "local" / "universe.yaml"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


class _Json(DefaultJSONProvider):
    ensure_ascii = False
    sort_keys = False

    @staticmethod
    def default(o: Any) -> Any:           # Decimal 一律输出规范十进制字符串（绝不转 float）
        if isinstance(o, Decimal):
            return to_db(o)
        if isinstance(o, (datetime,)):
            return iso_utc(o)
        if isinstance(o, date):
            return o.isoformat()
        if isinstance(o, (set, frozenset)):
            return sorted(o)
        return DefaultJSONProvider.default(o)


@dataclass
class WebState:
    config: Config
    entries: dict[str, registry.ViewEntry]
    problems: list[registry.ViewProblem]
    clock: Callable[[], datetime]
    universe_path: Path | None
    order: list[str] = field(default_factory=list)


def _universe_path(config: Config, override: Any) -> Path | None:
    if override is not None:
        return Path(override) if override else None
    cfg_path = (config.raw.get("universe") or {}).get("path") if isinstance(config.raw.get("universe"), dict) else None
    if cfg_path:
        p = Path(cfg_path)
        return p if p.is_absolute() else REPO_ROOT / p
    return DEFAULT_UNIVERSE if DEFAULT_UNIVERSE.exists() else None


def create_app(config: Config, *, extra_views_dirs: list[Path] | tuple[Path, ...] = (), views_config: Path | str | dict | None = DEFAULT_VIEWS_CONFIG,
               clock: Callable[[], datetime] | None = None, universe_path: Path | str | None = None) -> Flask:
    """创建应用。`universe_path`：标的名单（只用于给持仓标「核心/交易」角色）；传空字符串表示明确不用名单。"""
    if not is_loopback(config.web.host):
        raise ConfigError(f"web.host 必须是回环地址：{config.web.host!r}")
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.json = _Json(app)
    specs, problems = registry.discover_views([registry.BUILTIN_VIEWS_DIR, *map(Path, extra_views_dirs)])
    ordered, cfg_problems = registry.apply_config(specs, registry.load_views_config(views_config))
    state = WebState(config, {e.spec.id: e for e in ordered}, problems + cfg_problems, clock or utc_now,
                     _universe_path(config, universe_path), [e.spec.id for e in ordered])
    app.extensions["mystock2"] = state

    @app.before_request
    def _guard() -> Response | None:
        if _hostname(request.host or "").lower() not in LOOPBACK_HOSTS:
            return _err(403, "bad_host", "只接受回环地址访问")
        return None

    @app.after_request
    def _headers(resp: Response) -> Response:
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/") or request.path == "/" or request.path.endswith("/panel.js"):
            resp.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"            # 每次校验（ETag）：更新后不会拿旧的 ui.js 去配新的面板脚本
        return resp

    @app.get("/")
    def index() -> Response:
        return send_file(STATIC_DIR / "index.html", mimetype="text/html")

    @app.get("/api/views")
    def api_views() -> Response:
        return jsonify({
            "views": [_public(state.entries[i]) for i in state.order if state.entries[i].enabled],
            "problems": [p.public() for p in state.problems],
            "db": _db_status(state.config.db_path),
            "bind": {"host": state.config.web.host, "port": state.config.web.port},
        })

    @app.get("/api/v/<view_id>")
    def api_view(view_id: str) -> tuple[Response, int] | Response:
        entry = state.entries.get(view_id)
        if entry is None:
            return _err(404, "unknown_view", f"没有这个视图：{view_id}")
        if not entry.enabled:
            return _err(404, "view_disabled", f"视图已在 views.yaml 中停用：{view_id}")
        return _run_view(state, entry)

    @app.get("/views/<view_id>/panel.js")
    def view_panel(view_id: str) -> Response:
        entry = state.entries.get(view_id)
        if entry is None or not entry.enabled or not entry.spec.has_panel:
            return _err(404, "no_panel", f"没有面板脚本：{view_id}")
        return send_file(entry.spec.dir / "panel.js", mimetype="application/javascript")

    @app.post("/api/review/deal")
    def api_review_deal() -> tuple[Response, int] | Response:
        """拉起 `mystock2 review deal`（后台进程）。防跨站：必须带自定义头、同源、回环 Host。"""
        if request.headers.get("X-MyStock2-Action") != "review":
            return _err(403, "bad_request", "缺少请求头 X-MyStock2-Action")
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return _err(403, "bad_origin", "只接受同源请求")
        if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            return _err(403, "bad_origin", "只接受同源请求")
        body = request.get_json(silent=True) or {}
        deal_id, refresh = str(body.get("deal_id") or "").strip(), bool(body.get("refresh"))
        if not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,64}", deal_id):
            return _err(400, "bad_param", "deal_id 不合法")
        try:
            conn = dbmod.connect_ro(state.config.db_path)
        except dbmod.DbError:
            return _err(503, "db_missing", "库不存在，请先 db migrate")
        try:
            acct = conn.execute("SELECT account_id FROM account ORDER BY account_id LIMIT 1").fetchone()
            account = str(body.get("account") or "").strip() or (acct["account_id"] if acct else "")
            if not conn.execute("SELECT 1 FROM ledger_event WHERE account_id=? AND ref_deal_id=? AND event_type='FILL' LIMIT 1", (account, deal_id)).fetchone():
                return _err(404, "no_such_deal", "找不到这笔成交")
            last, ok = review_cache.latest(conn, deal_id), review_cache.latest_ok(conn, deal_id)
        except sqlite3.OperationalError:
            return _err(503, "no_review_table", "库还没有应用迁移 0013（python -m mystock2 db migrate）")
        finally:
            conn.close()
        if review_cache.is_running(last, state.clock()):
            return jsonify({"state": "running"}), 200
        if ok is not None and not refresh:
            return jsonify({"state": "ok"}), 200
        argv = [sys.executable, "-m", "mystock2"] + (["--config", str(state.config.source)] if state.config.source else []) + \
               ["review", "deal", "--deal-id", deal_id, "--account-id", account] + (["--refresh"] if refresh else [])
        spawn = app.extensions.get("mystock2_review_spawn") or _spawn_review
        try:
            spawn(argv)
        except OSError as exc:
            return _err(500, "spawn_failed", f"无法启动后台进程：{exc}")
        return jsonify({"state": "running"}), 202

    return app


def _spawn_review(argv: list[str]) -> None:
    """后台拉起 CLI（独立会话，Web 重启/请求结束都不影响它）；输出追加到 data/logs/review.log。"""
    log_dir = REPO_ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / "review.log", "ab")                       # noqa: SIM115 —— 子进程继承，随进程结束关闭
    subprocess.Popen(argv, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    out.close()


def _hostname(host: str) -> str:
    return host[: host.index("]") + 1] if host.startswith("[") and "]" in host else host.split(":")[0]


def _public(e: registry.ViewEntry) -> dict:
    s = e.spec
    return {"id": s.id, "title": s.title, "description": s.description, "group": s.group, "hidden": e.hidden, "data_mode": s.data_mode, "has_panel": s.has_panel,
            "params": [dict(p.public(), default=e.defaults.get(p.name, p.default)) for p in s.params.values()]}


def _err(status: int, code: str, message: str) -> tuple[Response, int]:
    return jsonify({"status": "error", "error": {"code": code, "message": message}}), status


def _db_status(path: Path) -> dict:
    if not Path(path).exists():
        return {"state": "missing", "message": "库不存在，请先 db migrate（python -m mystock2 db migrate）"}
    return {"state": "present"}


def _envelope(state: WebState, entry: registry.ViewEntry, params: dict, status: str, now: datetime, *, data: Any = None, fresh: dict | None = None,
              error: dict | None = None) -> dict:
    header = build_header(entry.spec.data_mode, fresh, now, entry.spec.stale_after_hours)
    public_params = {k: v for k, v in params.items() if not k.startswith("_")}
    return {"view_id": entry.spec.id, "title": entry.spec.title, "status": status, "params": public_params, "header": header,
            "data": data, "error": error}


def _run_view(state: WebState, entry: registry.ViewEntry) -> tuple[Response, int] | Response:
    now = state.clock()
    try:
        params = entry.coerce_params(request.args)
    except registry.ViewError as exc:
        return _err(400, "bad_param", str(exc))
    params["_now"] = now
    params["_universe_path"] = state.universe_path

    def unavailable(code: str, message: str) -> Response:
        return jsonify(_envelope(state, entry, params, "unavailable", now, error={"code": code, "message": message}))

    try:
        conn = dbmod.connect_ro(state.config.db_path)
    except dbmod.DbError:
        return unavailable("db_missing", "库不存在，请先 db migrate（python -m mystock2 db migrate）")
    except sqlite3.Error as exc:
        return unavailable("db_unreadable", f"数据库无法以只读方式打开：{exc}")
    try:
        result = entry.spec.run(conn, params)
        if not isinstance(result, dict):
            raise TypeError("query.run 必须返回 dict")
        fresh = result.pop("_freshness", None)
        names.annotate(conn, result)                              # 标的中文名（展示用；查不到不报错）
        return jsonify(_envelope(state, entry, params, "ok", now, data=result, fresh=fresh))
    except ViewUnavailable as exc:
        return unavailable(exc.code, exc.message)
    except registry.ViewError as exc:                           # 视图自己校验的参数（如 stock 的 code）不合法
        return _err(400, "bad_param", str(exc))
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc) or "no such column" in str(exc):
            return unavailable("schema_missing", "库结构不完整，请先 db migrate")
        log.exception("view %s 查询失败", entry.spec.id)
        return _err(500, "query_failed", f"查询失败：{exc}")
    except Exception as exc:                                    # noqa: BLE001 — 单个视图失败不得拖垮整页
        log.exception("view %s 查询失败", entry.spec.id)
        return _err(500, "query_failed", f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()


def serve(config: Config, **kwargs: Any) -> None:
    """启动开发服务（仅回环、不开 debug、不自动重载）。"""
    app = create_app(config, **kwargs)
    app.run(host=config.web.host, port=config.web.port, debug=False, use_reloader=False, threaded=True)
