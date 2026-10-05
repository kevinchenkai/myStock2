-- 0002 ledger：事件账本（实施方案 §6、§6 账本不变量；M2a）。
-- 所有金额/数量均为十进制字符串（TEXT）。账本相关表一律追加；用触发器在库层拒绝 UPDATE/DELETE。

CREATE TABLE account (
    account_id  TEXT PRIMARY KEY,          -- 内部稳定标识（不是券商账号）
    broker      TEXT NOT NULL,
    trd_env     TEXT NOT NULL CHECK (trd_env IN ('REAL','SIMULATE')),
    base_ccy    TEXT,
    note        TEXT
);

-- 开账：一个账户只有一个开账点 t0（实施方案 §6 不变量 1）
CREATE TABLE account_opening (
    account_id  TEXT PRIMARY KEY REFERENCES account(account_id),
    opening_at  TEXT NOT NULL,             -- t0（UTC）
    snapshot_id TEXT,
    created_at  TEXT NOT NULL
);

-- 原始来源证据：不可变；与规范事件多对多（source_link）
CREATE TABLE source_record (
    source_record_id TEXT PRIMARY KEY,     -- sha256(source|source_id|content_hash)
    source           TEXT NOT NULL,        -- futu | v1 | csv | manual
    source_id        TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    raw_ref          TEXT,                 -- 指向本地私有原文，不放账号
    received_at      TEXT NOT NULL,
    UNIQUE (source, source_id, content_hash)
);

-- 规范业务事件：只追加。business_key 跨版本、跨来源稳定；更正＝REVERSAL + 新版本事件
CREATE TABLE ledger_event (
    event_id            TEXT PRIMARY KEY,  -- business_key || '#' || event_version
    business_key        TEXT NOT NULL,
    event_version       INTEGER NOT NULL CHECK (event_version >= 1),
    account_id          TEXT NOT NULL REFERENCES account(account_id),
    event_type          TEXT NOT NULL CHECK (event_type IN (
        'OPENING_POSITION','OPENING_CASH','FILL','FEE','DIVIDEND_ACCRUAL','DIVIDEND_PAYMENT',
        'DIVIDEND_SHORTFALL','DEPOSIT','WITHDRAW','FX','INTEREST','TAX','ADJUST','REVERSAL')),
    event_at            TEXT NOT NULL,     -- 事件发生时间（UTC）
    received_at         TEXT NOT NULL,
    market              TEXT,
    code                TEXT,
    currency            TEXT NOT NULL,
    price               TEXT,              -- FILL 的成交价（校验用）
    qty_delta           TEXT NOT NULL DEFAULT '0',
    cash_delta          TEXT NOT NULL DEFAULT '0',
    recv_delta          TEXT NOT NULL DEFAULT '0',   -- 应收（股息）变动
    attrib_amount       TEXT,              -- 非现金归因金额（DIVIDEND_SHORTFALL）
    ref_deal_id         TEXT,
    ref_order_id        TEXT,
    ref_event_key       TEXT,              -- 归属的业务事件（如税款归属某次股息）
    group_id            TEXT,              -- 多腿事件（FX）、股息支付组
    leg_id              TEXT,
    corrects_event_id   TEXT REFERENCES ledger_event(event_id),
    correction_request_id TEXT,
    adjust_class        TEXT CHECK (adjust_class IN ('EXTERNAL_FLOW','INVESTMENT','OTHER') OR adjust_class IS NULL),
    note                TEXT,
    content_hash        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (business_key, event_version)
);
CREATE INDEX idx_event_account_time ON ledger_event(account_id, event_at);
CREATE INDEX idx_event_group ON ledger_event(group_id);
CREATE INDEX idx_event_request ON ledger_event(correction_request_id);
-- 同一旧版本不得被冲销两次（实施方案 §6 不变量 7）
CREATE UNIQUE INDEX uq_reversal_once ON ledger_event(corrects_event_id) WHERE event_type = 'REVERSAL';

CREATE TABLE source_link (
    source_record_id TEXT NOT NULL REFERENCES source_record(source_record_id),
    event_id         TEXT NOT NULL REFERENCES ledger_event(event_id),
    PRIMARY KEY (source_record_id, event_id)
);

-- 身份不足、无法归并的来源记录：进入待匹配队列，不入账；解决以追加一行 pending_resolution 表示
CREATE TABLE pending_match (
    pending_id       TEXT PRIMARY KEY,
    source_record_id TEXT NOT NULL REFERENCES source_record(source_record_id),
    reason           TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE pending_resolution (
    pending_id   TEXT PRIMARY KEY REFERENCES pending_match(pending_id),
    resolution   TEXT NOT NULL CHECK (resolution IN ('posted','duplicate','rejected')),
    event_id     TEXT,
    note         TEXT,
    resolved_at  TEXT NOT NULL
);

-- 公司行动：拆股以因子保存（不是固定数量增量）
CREATE TABLE corporate_action (
    action_id    TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('SPLIT')),
    market       TEXT NOT NULL,
    code         TEXT NOT NULL,
    ratio_num    INTEGER NOT NULL CHECK (ratio_num > 0),   -- 1 股变 ratio_num/ratio_den 股
    ratio_den    INTEGER NOT NULL CHECK (ratio_den > 0),
    effective_at TEXT NOT NULL,
    source       TEXT,
    created_at   TEXT NOT NULL
);

-- 费用档案（费率数值属私有；本表在私有库中）
CREATE TABLE fee_profile (
    profile_id  TEXT NOT NULL,
    market      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('BUY','SELL','ANY')),
    basis       TEXT NOT NULL CHECK (basis IN ('order','fill','period')),
    currency    TEXT NOT NULL,
    pct_fee     TEXT NOT NULL DEFAULT '0',
    min_fee     TEXT NOT NULL DEFAULT '0',
    flat_fee    TEXT NOT NULL DEFAULT '0',
    cap_fee     TEXT,
    tax_pct     TEXT NOT NULL DEFAULT '0',
    round_step  TEXT NOT NULL DEFAULT '0.01',
    valid_from  TEXT NOT NULL,
    valid_to    TEXT,
    source      TEXT,
    PRIMARY KEY (profile_id, market, side, valid_from)
);

-- 券商快照：持仓与逐币种现金，只追加，供对账与开账
CREATE TABLE account_snapshot (
    snapshot_id        TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL REFERENCES account(account_id),
    captured_at        TEXT NOT NULL,
    source             TEXT NOT NULL,
    source_record_id   TEXT REFERENCES source_record(source_record_id)
);
CREATE TABLE snapshot_position (
    snapshot_id  TEXT NOT NULL REFERENCES account_snapshot(snapshot_id),
    market       TEXT NOT NULL,
    code         TEXT NOT NULL,
    qty          TEXT NOT NULL,
    sellable_qty TEXT,
    cost_basis   TEXT,
    PRIMARY KEY (snapshot_id, code)
);
CREATE TABLE snapshot_cash (
    snapshot_id TEXT NOT NULL REFERENCES account_snapshot(snapshot_id),
    currency    TEXT NOT NULL,
    cash        TEXT NOT NULL,
    available   TEXT,
    frozen      TEXT,
    PRIMARY KEY (snapshot_id, currency)
);

-- 追加语义：账本与证据类表在库层拒绝 UPDATE / DELETE
CREATE TRIGGER trg_ledger_event_no_update BEFORE UPDATE ON ledger_event BEGIN SELECT RAISE(ABORT, 'ledger_event 只追加'); END;
CREATE TRIGGER trg_ledger_event_no_delete BEFORE DELETE ON ledger_event BEGIN SELECT RAISE(ABORT, 'ledger_event 只追加'); END;
CREATE TRIGGER trg_source_record_no_update BEFORE UPDATE ON source_record BEGIN SELECT RAISE(ABORT, 'source_record 不可变'); END;
CREATE TRIGGER trg_source_record_no_delete BEFORE DELETE ON source_record BEGIN SELECT RAISE(ABORT, 'source_record 不可变'); END;
CREATE TRIGGER trg_source_link_no_update BEFORE UPDATE ON source_link BEGIN SELECT RAISE(ABORT, 'source_link 只追加'); END;
CREATE TRIGGER trg_source_link_no_delete BEFORE DELETE ON source_link BEGIN SELECT RAISE(ABORT, 'source_link 只追加'); END;
CREATE TRIGGER trg_snapshot_no_update BEFORE UPDATE ON account_snapshot BEGIN SELECT RAISE(ABORT, 'account_snapshot 只追加'); END;
CREATE TRIGGER trg_snapshot_no_delete BEFORE DELETE ON account_snapshot BEGIN SELECT RAISE(ABORT, 'account_snapshot 只追加'); END;
CREATE TRIGGER trg_snapshot_pos_no_update BEFORE UPDATE ON snapshot_position BEGIN SELECT RAISE(ABORT, 'snapshot_position 只追加'); END;
CREATE TRIGGER trg_snapshot_pos_no_delete BEFORE DELETE ON snapshot_position BEGIN SELECT RAISE(ABORT, 'snapshot_position 只追加'); END;
CREATE TRIGGER trg_snapshot_cash_no_update BEFORE UPDATE ON snapshot_cash BEGIN SELECT RAISE(ABORT, 'snapshot_cash 只追加'); END;
CREATE TRIGGER trg_snapshot_cash_no_delete BEFORE DELETE ON snapshot_cash BEGIN SELECT RAISE(ABORT, 'snapshot_cash 只追加'); END;
CREATE TRIGGER trg_corp_action_no_update BEFORE UPDATE ON corporate_action BEGIN SELECT RAISE(ABORT, 'corporate_action 只追加'); END;
CREATE TRIGGER trg_corp_action_no_delete BEFORE DELETE ON corporate_action BEGIN SELECT RAISE(ABORT, 'corporate_action 只追加'); END;
CREATE TRIGGER trg_pending_res_no_update BEFORE UPDATE ON pending_resolution BEGIN SELECT RAISE(ABORT, 'pending_resolution 只追加'); END;
CREATE TRIGGER trg_pending_res_no_delete BEFORE DELETE ON pending_resolution BEGIN SELECT RAISE(ABORT, 'pending_resolution 只追加'); END;
