-- 0003 market：行情、汇率、采集回执、不可变证据快照、证券规则、预测版本（实施方案 §6；M4）。
-- 行情被修订时追加新版本（不改旧行）；证据快照与预测版本不可覆盖。

CREATE TABLE quote_daily (
    code          TEXT NOT NULL,
    session_date  TEXT NOT NULL,           -- 市场本地交易日 YYYY-MM-DD
    version       INTEGER NOT NULL CHECK (version >= 1),
    source        TEXT NOT NULL,           -- yfinance | futu | ...
    open          TEXT NOT NULL,           -- 原始（未复权）价；复权价只用于特征，绝不进权益（实施方案 §6A.5）
    high          TEXT NOT NULL,
    low           TEXT NOT NULL,
    close         TEXT NOT NULL,
    adj_close     TEXT,                    -- 复权收盘价（仅特征）
    volume        TEXT,
    event_at      TEXT NOT NULL,           -- 该日收盘时间（UTC）
    received_at   TEXT NOT NULL,
    quality       TEXT NOT NULL CHECK (quality IN ('ok','partial','stale')),
    content_hash  TEXT NOT NULL,
    PRIMARY KEY (code, session_date, version)
);

CREATE TABLE quote_hourly (
    code          TEXT NOT NULL,
    bar_start     TEXT NOT NULL,           -- UTC
    version       INTEGER NOT NULL CHECK (version >= 1),
    bar_end       TEXT NOT NULL,
    source        TEXT NOT NULL,
    open          TEXT NOT NULL,
    high          TEXT NOT NULL,
    low           TEXT NOT NULL,
    close         TEXT NOT NULL,
    volume        TEXT,
    complete      INTEGER NOT NULL CHECK (complete IN (0,1)),   -- bar 是否已走完
    received_at   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    PRIMARY KEY (code, bar_start, version)
);

CREATE TABLE fx_rate (
    pair          TEXT NOT NULL,           -- 如 USDHKD（1 USD = rate HKD）
    rate_date     TEXT NOT NULL,
    version       INTEGER NOT NULL CHECK (version >= 1),
    source        TEXT NOT NULL,
    rate          TEXT NOT NULL,
    event_at      TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    PRIMARY KEY (pair, rate_date, version)
);

-- 采集回执：失败、空结果、陈旧一律保留，不记零
CREATE TABLE collection_log (
    attempt_id    TEXT PRIMARY KEY,
    run_id        TEXT,
    code          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('daily','hourly','fx')),
    source        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('ok','empty','error','stale','partial')),
    rows          INTEGER NOT NULL DEFAULT 0,
    detail        TEXT,
    attempted_at  TEXT NOT NULL
);

-- 不可变证据快照：行情/资料的输入快照 + 哈希，修订追加新快照
CREATE TABLE evidence_snapshot (
    snapshot_id   TEXT PRIMARY KEY,        -- = content_hash 前缀
    kind          TEXT NOT NULL,           -- daily_bars | hourly_bars | fx | note ...
    subject       TEXT NOT NULL,           -- 标的代码/币对
    content_json  TEXT NOT NULL,
    content_hash  TEXT NOT NULL UNIQUE,
    event_at      TEXT,
    available_at  TEXT,
    received_at   TEXT NOT NULL,
    time_trust    TEXT NOT NULL CHECK (time_trust IN ('exact','assumed_bar_end')),   -- 历史研究只能「按 bar 结束推定可得」时标 assumed
    created_at    TEXT NOT NULL
);

-- 证券规则（lot/tick），带有效期与来源；未知≠精确
CREATE TABLE security_rule (
    code          TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,
    lot_size      INTEGER CHECK (lot_size IS NULL OR lot_size > 0),
    tick_json     TEXT,                    -- [{"lt":"0.25","tick":"0.001"}, ..., {"tick":"0.01"}] 价格档位
    source        TEXT NOT NULL,
    verified      INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0,1)),
    created_at    TEXT NOT NULL,
    PRIMARY KEY (code, valid_from)
);

-- 预测版本：不可覆盖
CREATE TABLE prediction_version (
    prediction_id     TEXT PRIMARY KEY,    -- = content_hash 前缀
    code              TEXT NOT NULL,
    as_of_session     TEXT NOT NULL,       -- 数据截至的交易日 T
    target_session    TEXT NOT NULL,       -- 预测的交易日 T+1
    model_version     TEXT NOT NULL,
    feature_version   TEXT NOT NULL,
    params_json       TEXT NOT NULL,
    y_low             TEXT NOT NULL,       -- low_{T+1}/close_T − 1
    y_high            TEXT NOT NULL,
    low_price         TEXT NOT NULL,
    high_price        TEXT NOT NULL,
    scale             TEXT NOT NULL,
    n_train           INTEGER NOT NULL,
    input_snapshot_ids TEXT NOT NULL,      -- JSON 数组
    input_cutoff_at   TEXT NOT NULL,
    generated_at      TEXT NOT NULL,
    available_at      TEXT NOT NULL,
    source_tag        TEXT NOT NULL CHECK (source_tag IN ('forward','rebuilt')),
    content_hash      TEXT NOT NULL UNIQUE,
    created_at        TEXT NOT NULL
);
CREATE INDEX idx_pred_code_target ON prediction_version(code, target_session);

CREATE TRIGGER trg_evidence_no_update BEFORE UPDATE ON evidence_snapshot BEGIN SELECT RAISE(ABORT, 'evidence_snapshot 不可变'); END;
CREATE TRIGGER trg_evidence_no_delete BEFORE DELETE ON evidence_snapshot BEGIN SELECT RAISE(ABORT, 'evidence_snapshot 不可变'); END;
CREATE TRIGGER trg_pred_no_update BEFORE UPDATE ON prediction_version BEGIN SELECT RAISE(ABORT, 'prediction_version 不可覆盖'); END;
CREATE TRIGGER trg_pred_no_delete BEFORE DELETE ON prediction_version BEGIN SELECT RAISE(ABORT, 'prediction_version 不可覆盖'); END;
CREATE TRIGGER trg_quote_daily_no_update BEFORE UPDATE ON quote_daily BEGIN SELECT RAISE(ABORT, 'quote_daily 只追加新版本'); END;
CREATE TRIGGER trg_quote_daily_no_delete BEFORE DELETE ON quote_daily BEGIN SELECT RAISE(ABORT, 'quote_daily 只追加新版本'); END;
CREATE TRIGGER trg_quote_hourly_no_update BEFORE UPDATE ON quote_hourly BEGIN SELECT RAISE(ABORT, 'quote_hourly 只追加新版本'); END;
CREATE TRIGGER trg_quote_hourly_no_delete BEFORE DELETE ON quote_hourly BEGIN SELECT RAISE(ABORT, 'quote_hourly 只追加新版本'); END;
CREATE TRIGGER trg_fx_no_update BEFORE UPDATE ON fx_rate BEGIN SELECT RAISE(ABORT, 'fx_rate 只追加新版本'); END;
CREATE TRIGGER trg_fx_no_delete BEFORE DELETE ON fx_rate BEGIN SELECT RAISE(ABORT, 'fx_rate 只追加新版本'); END;
