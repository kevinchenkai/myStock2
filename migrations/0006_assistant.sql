-- 0006 assistant：LLM 否决包与调用记录（实施方案 §5 M9、§6A.3；人工通道）。
-- LLM 只做否决与解释：可取消/缩小买单、加风险标签；不得新增/放大/改限价、不得取消卖单、不得写账本或触达券商。

CREATE TABLE veto_packet (
    pack_id          TEXT PRIMARY KEY,           -- = 包内容哈希前缀
    batch_id         TEXT NOT NULL,
    line_id          TEXT NOT NULL,
    market           TEXT NOT NULL,
    target_session   TEXT NOT NULL,
    base_hashes      TEXT NOT NULL,              -- 绑定的基础单 frozen_hash（JSON，按 code 排序）
    evidence_ids     TEXT NOT NULL,              -- 包内证据 id（JSON）
    content_json     TEXT NOT NULL,              -- 实际外发的脱敏内容（白名单字段）
    field_whitelist  TEXT NOT NULL,              -- 外发字段清单（用于确认与审计）
    exported_at      TEXT NOT NULL
);

CREATE TABLE llm_call (
    call_id          TEXT PRIMARY KEY,
    pack_id          TEXT NOT NULL REFERENCES veto_packet(pack_id),
    provider         TEXT NOT NULL,              -- manual | api:<name>
    model_id         TEXT,                       -- 实际模型 ID（人工通道由用户填写）
    prompt_version   TEXT NOT NULL,
    input_hash       TEXT NOT NULL,
    output_json      TEXT,
    status           TEXT NOT NULL CHECK (status IN ('applied','rejected','invalid','closed')),
    reason           TEXT,
    imported_at      TEXT NOT NULL
);
-- 同一包只能被成功应用一次（重放防护）
CREATE UNIQUE INDEX uq_llm_applied_once ON llm_call(pack_id) WHERE status = 'applied';

CREATE TRIGGER trg_veto_packet_no_update BEFORE UPDATE ON veto_packet BEGIN SELECT RAISE(ABORT, 'veto_packet 不可改'); END;
CREATE TRIGGER trg_veto_packet_no_delete BEFORE DELETE ON veto_packet BEGIN SELECT RAISE(ABORT, 'veto_packet 不可删'); END;
CREATE TRIGGER trg_llm_call_no_update BEFORE UPDATE ON llm_call BEGIN SELECT RAISE(ABORT, 'llm_call 只追加'); END;
CREATE TRIGGER trg_llm_call_no_delete BEFORE DELETE ON llm_call BEGIN SELECT RAISE(ABORT, 'llm_call 只追加'); END;
