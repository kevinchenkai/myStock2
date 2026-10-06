"""请求一笔成交的 AI 评价并写缓存。由 `mystock2 review deal` 调用（受控 CLI；Web 只读缓存、只负责拉起本命令）。

规则：
- 有成功的缓存且未要求刷新 → 直接返回缓存，不调模型；
- 已有一个真在跑的请求 → 不重复发起；
- 刷新 → 新增一行（旧评价保留）；失败也留一行（status=error），不影响旧的成功评价；
- 发给模型的内容只含脱敏摘要（见 replay/review_payload.py）。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Callable

from mystock2.core import db as dbmod
from mystock2.core.timeutil import iso_utc, utc_now
from mystock2.replay import review_cache as rc
from mystock2.replay.review_payload import PROMPT_VERSION, input_hash, payload_for, render_prompt
from mystock2.review.codex_runner import DEFAULT_EFFORT, DEFAULT_MODEL, CodexError, run_codex


def settings(cfg_raw: dict) -> dict:
    r = cfg_raw.get("review") or {}
    return {"model": str(r.get("model") or DEFAULT_MODEL), "effort": str(r.get("effort") or DEFAULT_EFFORT),
            "timeout": float(r.get("timeout_s") or rc.DEFAULT_TIMEOUT_S), "codex_bin": r.get("codex_bin")}


def public_row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    return {k: r[k] for k in ("review_id", "deal_id", "code", "prompt_version", "input_hash", "model", "effort", "status", "response_text", "error",
                              "requested_at", "finished_at", "duration_s")}


def request_review(db_path, cfg_raw: dict, account_id: str, deal_id: str, *, refresh: bool = False,
                   runner: Callable[..., dict] = run_codex) -> dict:
    st = settings(cfg_raw)
    ro = dbmod.connect_ro(db_path)
    try:
        payload = payload_for(ro, account_id, deal_id)
        cached = rc.latest_ok(ro, deal_id)
    finally:
        ro.close()
    if payload is None:
        return {"state": "error", "error": "找不到这笔成交（或它是开账期初库存，没有复盘卡）"}
    h = input_hash(payload)
    if cached is not None and not refresh:
        return {"state": "ok", "cached": True, "review": public_row(cached), "stale": cached["input_hash"] != h}
    prompt = render_prompt(payload)
    now = utc_now()
    w = dbmod.connect_writer(db_path, "review")
    try:
        w.execute("BEGIN IMMEDIATE")                                   # 同一笔同时只允许一个在跑（检查与插入同一事务）
        last = rc.latest(w, deal_id)
        if rc.is_running(last, now, st["timeout"]):
            w.execute("ROLLBACK")
            return {"state": "running", "review": public_row(last)}
        cur = w.execute("INSERT INTO trade_review (deal_id, code, prompt_version, input_hash, model, effort, status, request_text, pid, requested_at) "
                        "VALUES (?,?,?,?,?,?,'running',?,?,?)",
                        (deal_id, payload["instrument"]["code"], PROMPT_VERSION, h, st["model"], st["effort"], prompt, os.getpid(), iso_utc(now)))
        rid = cur.lastrowid
        w.execute("COMMIT")
        try:
            res = runner(prompt, model=st["model"], effort=st["effort"], timeout=st["timeout"], codex_bin=st["codex_bin"])
        except CodexError as exc:
            res = {"ok": False, "text": "", "error": str(exc), "duration_s": 0.0}
        except Exception as exc:                                       # 任何意外都要落回执，不能让 running 悬着
            res = {"ok": False, "text": "", "error": f"{type(exc).__name__}: {exc}", "duration_s": 0.0}
        w.execute("UPDATE trade_review SET status=?, response_text=?, error=?, finished_at=?, duration_s=?, pid=NULL WHERE review_id=?",
                  ("ok" if res["ok"] else "error", res["text"] or None, res["error"], iso_utc(utc_now()), res.get("duration_s"), rid))
        row = w.execute("SELECT * FROM trade_review WHERE review_id=?", (rid,)).fetchone()
        return {"state": "ok" if res["ok"] else "error", "cached": False, "review": public_row(row), "stale": False, "error": res["error"]}
    finally:
        w.close()
