"""视图框架：自动发现、统一路由、views.yaml、坏视图隔离、库不存在/未迁移/无账户的业务状态（LN-01）。"""
import sqlite3
from pathlib import Path

import pytest
import yaml

from mystock2.core import db as dbmod
from mystock2.core.config import Config, ConfigError, FutuConfig, WebConfig
from mystock2.web import registry
from mystock2.web.app import create_app

from .test_web_fixtures import NOW, build_demo_db, get_view, make_app

BUILTIN = ["account_overview", "holdings", "trades", "pnl", "equity_trend", "fx", "tickets", "scoreboard", "forecast", "replay", "data_status"]


def write_view(root: Path, vid: str, query: str, *, meta: dict | None = None, panel: str | None = "MS.registerPanel('%s', function(){});"):
    d = root / vid
    d.mkdir(parents=True)
    (d / "view.yaml").write_text(yaml.safe_dump(meta or {"title": "示例视图", "order": 900, "data_mode": "daily"}, allow_unicode=True), encoding="utf-8")
    (d / "query.py").write_text(query, encoding="utf-8")
    if panel:
        (d / "panel.js").write_text(panel % vid if "%s" in panel else panel, encoding="utf-8")
    return d


DEMO_QUERY = '''
from mystock2.web import common as C

def run(conn, params):
    n = conn.execute("SELECT COUNT(*) AS n FROM ledger_event").fetchone()["n"]
    return {"events": n, "echo": params.get("who"), "_freshness": C.freshness([C.source("账本", None, None)])}
'''


def test_builtin_views_are_discovered_in_default_order(tmp_path):
    app = make_app(tmp_path)
    js = app.test_client().get("/api/views").get_json()
    assert [v["id"] for v in js["views"]] == BUILTIN
    assert js["problems"] == [] and all(v["has_panel"] for v in js["views"])
    assert js["bind"] == {"host": "127.0.0.1", "port": 8889}


def test_adding_a_view_is_just_adding_a_folder_LN01(tmp_path):
    """新增示例视图只需新增一个文件夹：不改核心代码，启动即被发现，路由与面板脚本自动可用。"""
    extra = tmp_path / "extra_views"
    write_view(extra, "demo_events", DEMO_QUERY, meta={"title": "示例视图", "order": 900, "params": {"who": {"type": "str", "default": "nobody"}}})
    app = make_app(tmp_path, extra_views_dirs=[extra])
    c = app.test_client()
    ids = [v["id"] for v in c.get("/api/views").get_json()["views"]]
    assert ids == BUILTIN + ["demo_events"]
    code, body = get_view(c, "demo_events", who="kk")
    assert code == 200 and body["status"] == "ok"
    assert body["data"]["events"] > 0 and body["data"]["echo"] == "kk" and "_freshness" not in body["data"]
    assert body["header"]["staleness"]["label"] == "未知"                 # 视图未给时间 → 「未知」，不是「新鲜」
    js = c.get("/views/demo_events/panel.js")
    assert js.status_code == 200 and b"registerPanel" in js.data and js.mimetype == "application/javascript"
    assert c.get("/views/nope/panel.js").status_code == 404


def test_query_run_is_pure_function_of_connection_and_params(tmp_path):
    from mystock2.web.views.fx import query as fxq
    db = build_demo_db(tmp_path)
    conn = dbmod.connect_ro(db)
    a = fxq.run(conn, {"_now": NOW, "pair": "USDHKD", "days": 30, "max_stale_days": 4})
    b = fxq.run(conn, {"_now": NOW, "pair": "USDHKD", "days": 30, "max_stale_days": 4})
    assert a == b
    with pytest.raises(sqlite3.OperationalError):               # 视图拿到的是只读连接：写入必败
        conn.execute("INSERT INTO run_log(run_id, command, started_at, status) VALUES ('x','y','z','ok')")


def test_view_with_forbidden_imports_is_rejected_but_app_keeps_serving(tmp_path):
    extra = tmp_path / "extra"
    for vid, bad in (("bad_net", "import socket\n"), ("bad_writer", "from mystock2.core.db import connect_writer\n"),
                     ("bad_train", "import mystock2.forecast.baseline\n"), ("bad_clock", "from mystock2.core.timeutil import utc_now\nx = utc_now()\n")):
        write_view(extra, vid, bad + "def run(conn, params):\n    return {}\n")
    write_view(extra, "no_run", "x = 1\n")
    write_view(extra, "broken", "def run(:\n")
    app = make_app(tmp_path, extra_views_dirs=[extra])
    c = app.test_client()
    js = c.get("/api/views").get_json()
    assert [v["id"] for v in js["views"]] == BUILTIN
    probs = {p["view"]: p["message"] for p in js["problems"]}
    assert set(probs) == {"bad_net", "bad_writer", "bad_train", "bad_clock", "no_run", "broken"}
    assert "socket" in probs["bad_net"] and "connect_writer" in probs["bad_writer"] and "forecast" in probs["bad_train"] and "utc_now" in probs["bad_clock"]
    assert get_view(c, "fx")[0] == 200 and get_view(c, "bad_net")[0] == 404


def test_builtin_view_sources_pass_the_same_static_lint():
    for q in registry.BUILTIN_VIEWS_DIR.glob("*/query.py"):
        assert registry.lint_query_source(q) == [], q


def test_views_yaml_controls_enabled_hidden_order_and_default_params(tmp_path):
    cfg = {"views": [
        {"id": "fx", "params": {"days": 30}},
        {"id": "holdings", "hidden": True},
        {"id": "trades", "enabled": False},
        {"id": "ghost"},
        {"id": "pnl", "params": {"nope": 1}},
    ]}
    app = make_app(tmp_path, views_config=cfg)
    c = app.test_client()
    js = c.get("/api/views").get_json()
    ids = [v["id"] for v in js["views"]]
    assert ids == ["fx", "holdings", "pnl", "account_overview", "equity_trend", "tickets", "scoreboard", "forecast", "replay", "data_status"]   # 列出的在前（按配置顺序），其余按 order；trades 已停用
    assert next(v for v in js["views"] if v["id"] == "holdings")["hidden"] is True
    fx = next(v for v in js["views"] if v["id"] == "fx")
    assert next(p for p in fx["params"] if p["name"] == "days")["default"] == 30
    assert {p["view"] for p in js["problems"]} == {"ghost", "pnl"}
    assert get_view(c, "trades")[0] == 404                                              # 停用
    assert get_view(c, "holdings")[0] == 200                                            # 隐藏但可访问
    assert get_view(c, "fx")[1]["params"]["days"] == 30                                 # 默认参数生效
    assert get_view(c, "fx", days=60)[1]["params"]["days"] == 60                        # 请求参数覆盖默认


def test_shipped_views_yaml_is_valid_and_lists_every_builtin_view():
    cfg = registry.load_views_config(Path(registry.__file__).resolve().parents[2] / "config" / "views.yaml")
    assert [v["id"] for v in cfg["views"]] == BUILTIN


def test_param_validation_and_unknown_view(tmp_path):
    c = make_app(tmp_path).test_client()
    assert get_view(c, "account_overview", base_ccy="EUR")[0] == 400
    assert get_view(c, "trades", limit="abc")[0] == 400
    assert get_view(c, "trades", limit="0")[0] == 400
    code, body = get_view(c, "no_such_view")
    assert code == 404 and body["status"] == "error" and body["error"]["code"] == "unknown_view"


def test_envelope_has_header_and_data(tmp_path):
    c = make_app(tmp_path).test_client()
    code, body = get_view(c, "account_overview")
    assert code == 200 and set(body) >= {"view_id", "title", "status", "params", "header", "data", "error"}
    h = body["header"]
    assert set(h) >= {"data_mode", "data_mode_label", "event_at", "collected_at", "staleness", "sources", "generated_at"}
    assert h["staleness"]["label"] in ("新鲜", "陈旧", "未知") and h["generated_at"] == "2026-03-11T06:00:00.000000Z"
    assert all(not k.startswith("_") for k in body["params"])


def test_missing_database_gives_clear_business_status_not_a_crash(tmp_path):
    app = make_app(tmp_path, tmp_path / "does_not_exist.db")
    c = app.test_client()
    js = c.get("/api/views").get_json()
    assert js["db"]["state"] == "missing" and "db migrate" in js["db"]["message"]
    for v in js["views"]:
        code, body = get_view(c, v["id"])
        assert code == 200 and body["status"] == "unavailable" and body["error"]["code"] == "db_missing", v["id"]
        assert "db migrate" in body["error"]["message"] and body["header"]["staleness"]["label"] == "未知"
    assert not (tmp_path / "does_not_exist.db").exists()                 # Web 不创建数据库
    assert c.get("/").status_code == 200


def test_unmigrated_database_is_unavailable_schema_missing(tmp_path):
    p = tmp_path / "empty.db"
    sqlite3.connect(p).close()
    c = make_app(tmp_path, p).test_client()
    for vid in BUILTIN:
        code, body = get_view(c, vid)
        assert code == 200 and body["status"] == "unavailable" and body["error"]["code"] == "schema_missing", vid


def test_empty_migrated_database_reports_no_account(tmp_path):
    p = tmp_path / "m.db"
    dbmod.migrate(p)
    c = make_app(tmp_path, p).test_client()
    for vid in ("account_overview", "holdings", "trades", "pnl", "equity_trend", "replay"):
        code, body = get_view(c, vid)
        assert body["status"] == "unavailable" and body["error"]["code"] == "no_account", vid
    for vid in ("tickets", "scoreboard"):                                # 没有比较批次：业务状态，不是错误
        code, body = get_view(c, vid)
        assert body["status"] == "unavailable" and body["error"]["code"] == "no_batch", vid
    code, body = get_view(c, "data_status")                            # 数据状态不依赖账户：空库照常回答，新鲜度「未知」
    assert body["status"] == "ok" and body["header"]["staleness"]["label"] == "未知" and body["data"]["protocols"]["verdict"]["pilot"] is True
    code, body = get_view(c, "fx")                                      # 外汇不依赖账户：没有汇率 → 全部不可用
    assert body["status"] == "ok" and all(p["status"] == "unavailable" for p in body["data"]["paths"])
    assert body["header"]["staleness"]["label"] == "未知"


def test_unknown_account_param_is_a_business_state(tmp_path):
    code, body = get_view(make_app(tmp_path).test_client(), "holdings", account="ZZ")
    assert code == 200 and body["status"] == "unavailable" and body["error"]["code"] == "account_not_found"


def test_one_failing_view_does_not_break_others(tmp_path):
    extra = tmp_path / "extra"
    write_view(extra, "explodes", "def run(conn, params):\n    raise RuntimeError('boom')\n")
    c = make_app(tmp_path, extra_views_dirs=[extra]).test_client()
    code, body = get_view(c, "explodes")
    assert code == 500 and body["status"] == "error" and "boom" in body["error"]["message"]
    assert get_view(c, "holdings")[0] == 200


def test_only_get_and_loopback_host(tmp_path):
    c = make_app(tmp_path).test_client()
    assert c.post("/api/v/holdings").status_code == 405 and c.delete("/api/v/holdings").status_code == 405
    assert c.put("/").status_code == 405
    assert c.get("/api/views", headers={"Host": "evil.example.com"}).status_code == 403     # 防 DNS 重绑定
    assert c.get("/api/views", headers={"Host": "127.0.0.1:8889"}).status_code == 200
    assert c.get("/api/views", headers={"Host": "[::1]:8889"}).status_code == 200


def test_non_loopback_config_is_refused():
    cfg = Config(db_path=Path("x.db"), web=WebConfig("0.0.0.0", 8889), futu=FutuConfig("127.0.0.1", 11111, "REAL"), markets=("US",))
    with pytest.raises(ConfigError):
        create_app(cfg)


def test_security_headers_and_no_store(tmp_path):
    r = make_app(tmp_path).test_client().get("/api/v/fx")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "unsafe-inline" not in csp and "http" not in csp
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Content-Type-Options"] == "nosniff"


def test_json_never_uses_float_for_money(tmp_path):
    body = get_view(make_app(tmp_path).test_client(), "account_overview")[1]
    cell = body["data"]["currencies"][0]["cash"]
    assert isinstance(cell["v"], str) and cell["ccy"] in ("USD", "HKD")
