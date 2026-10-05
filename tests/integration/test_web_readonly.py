"""Web 只读保证（LN-01、NF-07、实施方案 §3.4）：遍历所有路由，断言数据库字节不变、无写入、无外部网络请求、无采集/训练。"""
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from mystock2.core import db as dbmod
from mystock2.core.config import REPO_ROOT
from tests.unit.test_web_fixtures import build_demo_db, make_app, make_config

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
WRITE_SQL = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "VACUUM", "ATTACH", "REINDEX", "ANALYZE")
EXTRA_VIEW = '''
from mystock2.web import common as C

def run(conn, params):
    return {"n": conn.execute("SELECT COUNT(*) AS n FROM ledger_event").fetchone()["n"],
            "_freshness": C.freshness([C.source("账本", None, None)])}
'''


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(path: Path):
    st = path.stat()
    return digest(path), st.st_size, st.st_mtime_ns


def side_files(path: Path):
    return {p.name: p.stat().st_size for p in path.parent.iterdir() if p.name.startswith(path.name + "-")}


def all_get_urls(app):
    """遍历 url_map 的每条路由，把 <view_id> 展开成每个视图，返回 (url, query) 列表。"""
    state = app.extensions["mystock2"]
    urls = []
    for rule in app.url_map.iter_rules():
        assert rule.methods - {"OPTIONS", "HEAD"} <= {"GET"}, f"{rule.rule} 允许了写方法：{rule.methods}"
        if rule.endpoint == "static":
            urls += [("/static/app.css", {}), ("/static/ui.js", {}), ("/static/nope.js", {})]
        elif "view_id" in rule.arguments:
            for vid in list(state.entries) + ["no_such_view"]:
                url = rule.rule.replace("<view_id>", vid)
                urls.append((url, {}))
                if rule.rule.startswith("/api/v/"):
                    urls += [(url, {"base_ccy": "HKD"}), (url, {"base_ccy": "CNY", "pair": "USDCNY"}), (url, {"code": "US.NVDA"}), (url, {"account": "zz"})]
        else:
            urls.append((rule.rule, {}))
    return urls


@pytest.fixture()
def env(tmp_path):
    db = build_demo_db(tmp_path)
    extra = tmp_path / "extra"
    (extra / "demo_view").mkdir(parents=True)
    (extra / "demo_view" / "view.yaml").write_text(yaml.safe_dump({"title": "示例视图"}, allow_unicode=True), encoding="utf-8")
    (extra / "demo_view" / "query.py").write_text(EXTRA_VIEW, encoding="utf-8")
    (extra / "demo_view" / "panel.js").write_text("MS.registerPanel('demo_view', function(){});", encoding="utf-8")
    return db, make_app(tmp_path, db, extra_views_dirs=[extra])


def test_every_route_leaves_database_bytes_unchanged_and_writes_nothing(env, monkeypatch):
    db, app = env
    before, before_side = snapshot(db), side_files(db)
    statements: list[str] = []
    opened = []
    real_connect_ro = dbmod.connect_ro

    def spy_connect_ro(path):
        conn = real_connect_ro(path)
        conn.set_trace_callback(statements.append)
        opened.append(conn)
        return conn

    def forbid(name):
        def _f(*a, **k):
            raise AssertionError(f"Web 请求期间不得调用 {name}")
        return _f

    monkeypatch.setattr(dbmod, "connect_ro", spy_connect_ro)
    for name in ("connect_writer", "connect_migrator", "migrate"):
        monkeypatch.setattr(dbmod, name, forbid(name))
    client = app.test_client()
    urls = all_get_urls(app)
    assert len(urls) > 40
    seen_status = set()
    for url, q in urls:
        r = client.get(url, query_string=q)
        seen_status.add(r.status_code)
        assert r.status_code < 500, (url, q, r.status_code, r.data[:200])
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)(url, query_string=q).status_code in (404, 405), (method, url)
    assert {200, 404} <= seen_status
    assert opened, "视图请求应当使用只读连接"
    bad = [s for s in statements if s.lstrip().upper().startswith(WRITE_SQL)]
    assert bad == [], bad[:5]
    assert snapshot(db) == before, "数据库文件在 Web 请求前后必须逐字节不变"
    after_side = side_files(db)
    assert all(after_side.get(k, 0) == v for k, v in before_side.items()) and all(v == 0 for k, v in after_side.items() if k.endswith("-wal"))


def test_no_network_calls_during_any_route(env, monkeypatch):
    db, app = env

    def deny(*a, **k):
        raise AssertionError("Web 请求期间出现网络访问")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    client = app.test_client()
    for url, q in all_get_urls(app):
        assert client.get(url, query_string=q).status_code < 500


def test_no_collection_or_training_modules_are_loaded_by_the_web(env, tmp_path):
    """在全新的解释器里 import Web、遍历所有路由，然后检查采集/训练/联网模块根本没有被加载。"""
    db, app = env
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from tests.unit.test_web_fixtures import make_app
        from tests.integration.test_web_readonly import all_get_urls
        from pathlib import Path
        app = make_app(Path({str(tmp_path)!r}), Path({str(db)!r}))
        c = app.test_client()
        for url, q in all_get_urls(app):
            assert c.get(url, query_string=q).status_code < 500, url
        banned = ("mystock2.forecast", "mystock2.collectors", "mystock2.assistant", "yfinance", "futu", "requests", "urllib3", "numpy", "lightgbm", "sklearn")
        loaded = sorted(m for m in sys.modules if m.split(".")[0] in {{b.split(".")[0] for b in banned}} and any(m == b or m.startswith(b + ".") for b in banned))
        print(json.dumps(loaded))
    """)
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO_ROOT)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().splitlines()[-1] == "[]"


def test_read_only_connection_rejects_every_kind_of_write(env):
    db, _ = env
    conn = dbmod.connect_ro(db)
    for sql in ("INSERT INTO run_log(run_id, command, started_at, status) VALUES ('x','y','z','ok')",
                "UPDATE account SET note='x'", "DELETE FROM account", "CREATE TABLE evil(x)", "DROP TABLE account",
                "ALTER TABLE account ADD COLUMN y TEXT", "UPDATE ledger_event SET note='x'", "DELETE FROM quote_daily",
                "INSERT INTO fx_rate(pair, rate_date, version, source, rate, event_at, received_at, content_hash) VALUES ('A','b',1,'c','1','e','r','h')",
                "VACUUM", "PRAGMA journal_mode = DELETE"):
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(sql)
            conn.execute("COMMIT")
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    conn.close()


def test_view_queries_receive_a_read_only_connection(env):
    """视图拿到的连接写入必败：把一个试图写库的视图装进来，请求会得到 500（而不是改库）。"""
    db, app = env
    # 静态检查会拒绝 connect_writer 等名字，但直接执行写 SQL 的视图只能被只读连接挡住
    root = db.parent / "evil_views" / "writes"
    root.mkdir(parents=True)
    (root / "view.yaml").write_text("title: evil\n", encoding="utf-8")
    (root / "query.py").write_text("def run(conn, params):\n    conn.execute(\"DELETE FROM ledger_event\")\n    return {}\n", encoding="utf-8")
    from mystock2.web.app import create_app
    from tests.unit.test_web_fixtures import NOW
    app2 = create_app(make_config(db), extra_views_dirs=[db.parent / "evil_views"], views_config=None, clock=lambda: NOW, universe_path="")
    before = snapshot(db)
    r = app2.test_client().get("/api/v/writes")
    assert r.status_code == 500 and "readonly" in r.get_json()["error"]["message"].lower()
    assert snapshot(db) == before
    n = dbmod.connect_ro(db).execute("SELECT COUNT(*) FROM ledger_event").fetchone()[0]
    assert n > 0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(cfg: Path):
    return subprocess.Popen([sys.executable, "-m", "mystock2", "--config", str(cfg), "web"], cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_up(port: int, proc, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            raise AssertionError("web 进程提前退出：" + proc.stderr.read())
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/views", timeout=1) as r:
                return r.read()
        except Exception:
            time.sleep(0.2)
    raise AssertionError("web 进程未在时限内启动")


def write_cfg(tmp_path, db, port):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"futu": {"host": "127.0.0.1", "port": 11111, "trd_env": "REAL"}, "collect": {"markets": ["US"]},
                                   "db": {"path": str(db)}, "web": {"host": "127.0.0.1", "port": port}}), encoding="utf-8")
    return cfg


def test_cli_web_serves_only_on_loopback_and_is_read_only(tmp_path):
    db = build_demo_db(tmp_path)
    port = free_port()
    assert port not in (8888,)                                  # 绝不占用 V1 的端口
    cfg = write_cfg(tmp_path, db, port)
    before = snapshot(db)
    proc = start_server(cfg)
    try:
        wait_up(port, proc)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
            assert r.status == 200 and b"viewport" in r.read()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v/holdings", timeout=5) as r:
            assert r.status == 200 and json.loads(r.read())["status"] == "ok"
        lsof = shutil.which("lsof")
        if lsof:
            out = subprocess.run([lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
            assert f"127.0.0.1:{port}" in out and "*:" + str(port) not in out and "0.0.0.0" not in out, out
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert snapshot(db) == before


def test_cli_web_with_missing_database_starts_and_explains(tmp_path):
    db = tmp_path / "nope.db"
    port = free_port()
    proc = start_server(write_cfg(tmp_path, db, port))
    try:
        body = wait_up(port, proc)
        assert b"db migrate" in body
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v/account_overview", timeout=5) as r:
            assert b"db_missing" in r.read()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert not db.exists()                                      # Web 不会创建库
    assert os.path.exists(tmp_path) and not list(tmp_path.glob("*.db*"))
