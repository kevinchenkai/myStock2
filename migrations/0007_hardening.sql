-- 0007 hardening（代码评审后加固）：eval_run 冻结后只读；仅允许在 metrics_json 尚为空时一次性写入指标。

CREATE TRIGGER trg_eval_run_freeze BEFORE UPDATE ON eval_run
WHEN OLD.metrics_json IS NOT NULL
   OR NEW.run_id IS NOT OLD.run_id OR NEW.batch_id IS NOT OLD.batch_id OR NEW.protocol_version IS NOT OLD.protocol_version
   OR NEW.protocol_json IS NOT OLD.protocol_json OR NEW.evidence_json IS NOT OLD.evidence_json OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'eval_run 冻结后只读（仅可一次性写入 metrics_json）'); END;
