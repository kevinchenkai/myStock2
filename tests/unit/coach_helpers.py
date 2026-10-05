"""教练/否决测试：写入合成的前向预测行（操作单的 model_ref 必须引用真实存在的前向预测）。"""
from __future__ import annotations

from mystock2.core import db as dbmod


def seed_prediction(path, prediction_id: str, code: str, target: str, source_tag: str = "forward") -> None:
    w = dbmod.connect_writer(path, "forecast")
    w.execute(
        "INSERT OR IGNORE INTO prediction_version(prediction_id, code, as_of_session, target_session, model_version, feature_version, params_json, y_low, y_high, "
        "low_price, high_price, scale, n_train, input_snapshot_ids, input_cutoff_at, generated_at, available_at, source_tag, content_hash, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (prediction_id, code, "2026-03-04", target, "m", "f", "{}", "-0.05", "0.05", "95", "105", "0.02", 250, "[]", "2026-03-04T22:00:00.000000Z",
         "2026-03-04T22:01:00.000000Z", "2026-03-04T22:01:00.000000Z", source_tag, "h-" + prediction_id, "2026-03-04T22:01:00.000000Z"))
    w.close()
