"""预测版本留档（不可覆盖；WP4.6、F07 时间链）。

写入前校验：输入证据快照的 `received_at ≤ input_cutoff_at ≤ generated_at ≤ available_at`；
违规则拒绝写入（不是写进去再标记）。`source_tag`：forward（前向，当时生成）| rebuilt（事后重建），二者不得混算正式指标。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date

from mystock2.core import calendars as cal
from mystock2.core.db import atomic
from mystock2.core.timeutil import EvidenceTimes, check_time_chain, ensure_utc, iso_utc, utc_now
from mystock2.forecast.baseline import FEATURE_VERSION, MODEL_VERSION, Prediction, prediction_fields
from mystock2.instruments.code_map import market_of
from mystock2.market.evidence import verify_inputs


class VersionError(ValueError):
    pass


def existing_forward(conn: sqlite3.Connection, code: str, as_of_session: date, model_version: str, feature_version: str, params: dict) -> str | None:
    """同一标的、同一数据截至日、同一模型/特征版本/参数的前向预测只留第一条（重复运行、重试、补跑都不会再写）。
    前向的意义是「当时」的预测，后来的重复只会稀释样本、让逐日对账出现多条；参数不同（如教练协议参数）视为另一条，不互相顶替。"""
    row = conn.execute(
        "SELECT prediction_id FROM prediction_version WHERE source_tag='forward' AND code=? AND as_of_session=? AND model_version=? "
        "AND feature_version=? AND params_json=? ORDER BY generated_at LIMIT 1",
        (code, as_of_session.isoformat(), model_version, feature_version, json.dumps(params, sort_keys=True))).fetchone()
    return row[0] if row else None


def record_prediction(conn: sqlite3.Connection, code: str, pred: Prediction, params, input_snapshot_ids: list[str],
                      *, input_cutoff_at, generated_at, available_at, source_tag: str, model_version: str = MODEL_VERSION,
                      feature_version: str = FEATURE_VERSION) -> str:
    if source_tag not in ("forward", "rebuilt"):
        raise VersionError("source_tag 必须是 forward 或 rebuilt")
    market = market_of(code)
    target = cal.next_session(market, pred.as_of_session)
    problems = verify_inputs(conn, input_snapshot_ids, input_cutoff_at)
    problems += [f"chain:{p}" for p in check_time_chain(EvidenceTimes(
        received_at=ensure_utc(input_cutoff_at), input_cutoff_at=input_cutoff_at, generated_at=generated_at,
        frozen_at=available_at, deadline_at=available_at))]
    if source_tag == "forward" and not input_snapshot_ids:
        problems.append("forward_requires_evidence")
    if source_tag == "forward":                      # 前向＝当时生成（审核 P1-1）：输入截止不早于 T 日收盘，生成早于目标日开盘
        if ensure_utc(input_cutoff_at) < cal.session(market, pred.as_of_session).close_utc:
            problems.append("forward_cutoff_before_as_of_close")
        if ensure_utc(generated_at) >= cal.session(market, target).open_utc:
            problems.append("forward_generated_after_target_open（事后生成的预测只能标 rebuilt）")
    if problems:
        raise VersionError("拒绝写入预测版本：" + "; ".join(problems))
    fields = prediction_fields(pred)
    body = {"code": code, "as_of_session": pred.as_of_session.isoformat(), "target_session": target.isoformat(),
            "model_version": model_version, "feature_version": feature_version, "params": params.as_dict(), "fields": fields,
            "inputs": sorted(input_snapshot_ids), "input_cutoff_at": iso_utc(input_cutoff_at), "source_tag": source_tag}
    h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    pid = h[:24]
    with atomic(conn):
        if conn.execute("SELECT 1 FROM prediction_version WHERE prediction_id=?", (pid,)).fetchone():
            return pid
        if source_tag == "forward":
            first = existing_forward(conn, code, pred.as_of_session, model_version, feature_version, params.as_dict())
            if first:
                return first
        conn.execute(
            "INSERT INTO prediction_version(prediction_id, code, as_of_session, target_session, model_version, feature_version, params_json, y_low, y_high, "
            "low_price, high_price, scale, n_train, input_snapshot_ids, input_cutoff_at, generated_at, available_at, source_tag, content_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, code, body["as_of_session"], body["target_session"], model_version, feature_version, json.dumps(params.as_dict(), sort_keys=True),
             fields["y_low"], fields["y_high"], fields["low_price"], fields["high_price"], fields["scale"], fields["n_train"],
             json.dumps(sorted(input_snapshot_ids)), iso_utc(input_cutoff_at), iso_utc(generated_at), iso_utc(available_at), source_tag, h, iso_utc(utc_now())))
    return pid


def latest_for_target(conn: sqlite3.Connection, code: str, target_session: date, source_tag: str | None = None) -> sqlite3.Row | None:
    sql = "SELECT * FROM prediction_version WHERE code=? AND target_session=?"
    args: list = [code, target_session.isoformat()]
    if source_tag:
        sql += " AND source_tag=?"
        args.append(source_tag)
    return conn.execute(sql + " ORDER BY generated_at DESC LIMIT 1", args).fetchone()
