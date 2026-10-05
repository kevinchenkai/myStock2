-- 0009：券商订单（意图）、标的名称/档案、盘前价、资金流向、V1 前向预测存档。
-- 订单是「意图」，成交才是事实：订单只用于复盘与行为分析，不进入账本和式（账本只认成交/费用/资金流水）。

-- 券商订单：富途与 V1 的订单表（含已撤、失败）。状态会变化，存最新状态＋首次看到时间；来源列区分 futu / v1。
CREATE TABLE broker_order (
    account_id      TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    market          TEXT NOT NULL,
    code            TEXT NOT NULL,
    side            TEXT NOT NULL,                 -- BUY | SELL
    order_type      TEXT,
    status          TEXT NOT NULL,                 -- FILLED_ALL / CANCELLED_ALL / CANCELLED_PART / FAILED / SUBMITTED …
    price           TEXT,
    qty             TEXT,
    dealt_qty       TEXT,
    dealt_avg_price TEXT,
    created_at      TEXT NOT NULL,                 -- UTC（V1 本地时间按市场补时区，推断）
    updated_at      TEXT,
    time_trust      TEXT NOT NULL DEFAULT 'exact', -- exact | assumed_local_tz
    source          TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    PRIMARY KEY (account_id, order_id)
);
CREATE INDEX idx_broker_order_code ON broker_order(code, created_at);

-- 标的名称（中文名等）：展示用参考数据；来源优先级 futu > v1。
CREATE TABLE instrument_name (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    source     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 标的档案（行业、市值、市盈率、52 周高低…）：描述性参考数据，数值按原样存文本；as_of 为来源给出的时刻。
CREATE TABLE instrument_profile (
    code           TEXT PRIMARY KEY,
    long_name      TEXT,
    sector         TEXT,
    industry       TEXT,
    exchange       TEXT,
    currency       TEXT,
    market_cap_mm  TEXT,
    shares_mm      TEXT,
    trailing_pe    TEXT,
    forward_pe     TEXT,
    price_to_book  TEXT,
    trailing_eps   TEXT,
    dividend_yield TEXT,
    beta           TEXT,
    week52_high    TEXT,
    week52_low     TEXT,
    lot_size       TEXT,
    website        TEXT,
    source         TEXT NOT NULL,
    as_of          TEXT,
    updated_at     TEXT NOT NULL
);

-- 盘前价（V1 的 yfinance 小时线盘前快照，带 available_at）：无法从 yfinance 回补，故原样迁移。
CREATE TABLE quote_preopen (
    code         TEXT NOT NULL,
    session_date TEXT NOT NULL,
    price        TEXT NOT NULL,
    prev_close   TEXT,
    available_at TEXT,
    source       TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    PRIMARY KEY (code, session_date, source)
);

-- 资金流向（日）：富途 capital flow（单位：来源货币金额）；V1 自 2025-07 起累积。
CREATE TABLE capital_flow_daily (
    code          TEXT NOT NULL,
    session_date  TEXT NOT NULL,
    in_flow       TEXT,
    main_in_flow  TEXT,
    super_in_flow TEXT,
    big_in_flow   TEXT,
    mid_in_flow   TEXT,
    sml_in_flow   TEXT,
    source        TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    PRIMARY KEY (code, session_date, source)
);

-- V1 前向预测存档（原样 JSON）：V1 在 2026-03 起逐日留档的预测/影子版本，保留作历史对照；不与 V2 的预测混算。
CREATE TABLE v1_prediction_archive (
    prediction_id  TEXT PRIMARY KEY,
    code           TEXT NOT NULL,
    as_of          TEXT NOT NULL,
    target_session TEXT,
    source         TEXT,                      -- live | backfill | recomputed | shadow_v1 | shadow_v2
    status         TEXT,
    generated_at   TEXT,
    published_at   TEXT,
    payload_json   TEXT NOT NULL,
    imported_at    TEXT NOT NULL
);
CREATE INDEX idx_v1_pred_code ON v1_prediction_archive(code, as_of);
