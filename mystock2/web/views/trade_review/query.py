"""复盘卡 AI 评价（只读）：返回这笔成交最新的缓存评价、是否在生成、输入是否已变化。

- 状态：none（还没评价过）／running（后台在请求）／ok（有评价）／error（最近一次请求失败；若此前有成功评价仍一并返回）。
- stale：缓存评价所用的输入与现在的输入不同（如 +5/+20 日结果到期了）——提示「可刷新」，不自动重发。
- Web 只读：本视图不调用模型、不写库；请求与刷新由 `mystock2 review deal` 完成。
"""
from __future__ import annotations

import sqlite3

from mystock2.replay import review_cache as rc
from mystock2.replay.review_payload import input_hash, payload_for
from mystock2.web import common as C
from mystock2.web.registry import ViewError


def _row(r: sqlite3.Row) -> dict:
    return {"review_id": r["review_id"], "text": r["response_text"], "model": r["model"], "effort": r["effort"], "prompt_version": r["prompt_version"],
            "requested_at": r["requested_at"], "finished_at": r["finished_at"], "duration_s": r["duration_s"]}


def run(conn, params):
    deal = (params.get("deal_id") or "").strip()
    if not deal:
        raise ViewError("必须提供 deal_id")
    now = C.now_of(params)
    acct, _ = C.resolve_account(conn, params)
    try:
        last, ok = rc.latest(conn, deal), rc.latest_ok(conn, deal)
        n = rc.history_count(conn, deal)
    except sqlite3.OperationalError:
        raise C.ViewUnavailable("no_review_table", "库还没有应用迁移 0013（python -m mystock2 db migrate）") from None
    running = rc.is_running(last, now)
    err = None                                                    # 最近一次请求的失败（可与较早的成功评价同时存在）
    if not running and last is not None and last["status"] == "running":
        err = "上一次请求被中断（后台进程已不在）"
    elif last is not None and last["status"] == "error":
        err = last["error"] or "请求失败"
    state = "running" if running else "ok" if ok is not None else "error" if err else "none"
    stale = None
    if ok is not None and not running:
        cur = payload_for(conn, acct["account_id"], deal)
        stale = None if cur is None else input_hash(cur) != ok["input_hash"]
    src = [C.source("AI 评价缓存", ok["finished_at"] if ok else None, ok["finished_at"] if ok else None)] if ok else []
    return {
        "deal_id": deal, "state": state, "review": _row(ok) if ok else None, "error": err, "stale": stale, "history_count": n,
        "running_since": last["requested_at"] if running else None, "account_id": acct["account_id"],
        "_freshness": C.freshness(src, ["评价由本机 Codex 生成，只含脱敏摘要；仅作复盘参考，不是投资建议"]),
    }
