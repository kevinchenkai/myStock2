-- 0005 coach：操作单、人类计划（意图）、暴露日志、协议冻结登记（实施方案 §6、§6A；M6）。
-- 操作单与意图冻结后不可改（触发器）；修改只能新增版本并指向旧单（supersedes）。

CREATE TABLE ticket (
    ticket_id        TEXT PRIMARY KEY,           -- = frozen_hash 前缀
    batch_id         TEXT NOT NULL,
    line_id          TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('line_sim','live_guidance')),
    market           TEXT NOT NULL,
    code             TEXT NOT NULL,
    target_session   TEXT NOT NULL,
    stage            TEXT NOT NULL CHECK (stage IN ('close','preopen','human_plan')),
    status           TEXT NOT NULL CHECK (status IN ('frozen','missed_deadline','unavailable')),
    action           TEXT NOT NULL CHECK (action IN ('BUY','SELL','HOLD','SKIP')),
    limit_price      TEXT,
    qty              TEXT,
    lot_size         INTEGER,
    reserved_cash    TEXT,
    valid_from       TEXT,
    valid_to         TEXT,
    reason_json      TEXT NOT NULL,
    invalidate_json  TEXT NOT NULL,
    uncertainty_json TEXT NOT NULL,
    model_ref        TEXT,                       -- prediction_id
    strategy_version TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    state_ref_type   TEXT NOT NULL CHECK (state_ref_type IN ('line_state','account_snapshot')),
    state_ref        TEXT NOT NULL,              -- 线内状态哈希 / 账户快照 id
    generated_at     TEXT NOT NULL,
    frozen_at        TEXT NOT NULL,
    visible_at       TEXT NOT NULL,              -- 冻结器写入；「可供查看」的唯一审计依据（§6A.3）
    deadline_at      TEXT NOT NULL,
    supersedes       TEXT REFERENCES ticket(ticket_id),
    frozen_hash      TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_ticket_cell ON ticket(batch_id, line_id, market, target_session, kind);

-- 人类计划（结构化）：决策前记录；不可改写（修改＝新记录）
CREATE TABLE intent (
    intent_id        TEXT PRIMARY KEY,
    batch_id         TEXT NOT NULL,
    line_id          TEXT NOT NULL,
    market           TEXT NOT NULL,
    code             TEXT NOT NULL,
    target_session   TEXT NOT NULL,
    action           TEXT NOT NULL CHECK (action IN ('BUY','SELL','HOLD','NO_TRADE')),
    limit_price      TEXT,
    qty              TEXT,
    valid_to         TEXT,
    state_hash       TEXT NOT NULL,              -- 记录时展示给用户的「人类线自己的状态」哈希
    recorded_at      TEXT NOT NULL,              -- 真实记录时间
    seen_ai          INTEGER NOT NULL CHECK (seen_ai IN (0,1)),
    late_record      INTEGER NOT NULL CHECK (late_record IN (0,1)),   -- 暴露/截止之后的补录
    note             TEXT,
    frozen_hash      TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_intent_cell ON intent(batch_id, line_id, market, target_session, code);

-- 暴露日志：任一通道首次可观测暴露即揭示（§6A.2）
CREATE TABLE intent_exposure (
    exposure_id      TEXT PRIMARY KEY,
    batch_id         TEXT NOT NULL,
    market           TEXT NOT NULL,
    target_session   TEXT NOT NULL,
    channel          TEXT NOT NULL,              -- coach_show | veto_export | other | self_reported
    revealed_at      TEXT NOT NULL,
    version_hashes   TEXT NOT NULL,              -- 被揭示的 ticket frozen_hash 数组
    note             TEXT
);
CREATE INDEX idx_exposure_cell ON intent_exposure(batch_id, market, target_session, revealed_at);

-- 协议冻结登记：真实协议在本地私有；这里只登记哈希与白名单摘要
CREATE TABLE protocol_freeze (
    protocol_version TEXT PRIMARY KEY,
    protocol_hash    TEXT NOT NULL,
    frozen_at        TEXT NOT NULL,
    summary_json     TEXT NOT NULL,
    code_sha         TEXT
);

CREATE TRIGGER trg_ticket_no_update BEFORE UPDATE ON ticket BEGIN SELECT RAISE(ABORT, 'ticket 冻结后不可改；修改请新增版本并指向旧单'); END;
CREATE TRIGGER trg_ticket_no_delete BEFORE DELETE ON ticket BEGIN SELECT RAISE(ABORT, 'ticket 不可删除'); END;
CREATE TRIGGER trg_intent_no_update BEFORE UPDATE ON intent BEGIN SELECT RAISE(ABORT, 'intent 不可改写'); END;
CREATE TRIGGER trg_intent_no_delete BEFORE DELETE ON intent BEGIN SELECT RAISE(ABORT, 'intent 不可删除'); END;
CREATE TRIGGER trg_exposure_no_update BEFORE UPDATE ON intent_exposure BEGIN SELECT RAISE(ABORT, 'intent_exposure 不可改写'); END;
CREATE TRIGGER trg_exposure_no_delete BEFORE DELETE ON intent_exposure BEGIN SELECT RAISE(ABORT, 'intent_exposure 不可删除'); END;
CREATE TRIGGER trg_protocol_no_update BEFORE UPDATE ON protocol_freeze BEGIN SELECT RAISE(ABORT, 'protocol_freeze 不可改'); END;
CREATE TRIGGER trg_protocol_no_delete BEFORE DELETE ON protocol_freeze BEGIN SELECT RAISE(ABORT, 'protocol_freeze 不可删除'); END;
