-- 0004 scoreboard：比较批次、策略线、线内状态、日度结果、模拟成交、运行成本（实施方案 §6；M5）。
-- 全部追加；冻结后的 run 只读（以新 run_id 重算，不覆盖）。

CREATE TABLE comparison_batch (
    batch_id          TEXT PRIMARY KEY,
    protocol_version  TEXT NOT NULL,
    start_date        TEXT NOT NULL,        -- 共同起点（交易日）
    currency          TEXT NOT NULL,
    e0                TEXT NOT NULL,        -- E_trade(t0)
    initial_state_json TEXT NOT NULL,       -- 完整状态包（现金、库存、成本批次、持有年龄、应收应付、结算、预留）
    state_hash        TEXT NOT NULL,
    lines_json        TEXT NOT NULL,        -- 参与线名单
    created_at        TEXT NOT NULL
);

CREATE TABLE strategy_line (
    line_id           TEXT PRIMARY KEY,
    batch_id          TEXT NOT NULL REFERENCES comparison_batch(batch_id),
    kind              TEXT NOT NULL,        -- human_actual | human_plan | buyhold | ai | ai_lgbm | ai_veto | ...
    params_hash       TEXT NOT NULL,
    protocol_version  TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE eval_run (
    run_id            TEXT PRIMARY KEY,
    batch_id          TEXT NOT NULL REFERENCES comparison_batch(batch_id),
    protocol_version  TEXT NOT NULL,
    protocol_json     TEXT NOT NULL,        -- ExecProtocol 全部参数（含费用档案引用与敏感性变体）
    evidence_json     TEXT NOT NULL,        -- 所用行情的证据快照 id
    metrics_json      TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE sleeve_daily (
    run_id            TEXT NOT NULL REFERENCES eval_run(run_id),
    line_id           TEXT NOT NULL REFERENCES strategy_line(line_id),
    currency          TEXT NOT NULL,
    date              TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('OK','UNKNOWN','PAUSED')),
    equity            TEXT,                 -- UNKNOWN/PAUSED 时为 NULL（不记零）
    cash              TEXT,
    unsettled         TEXT,
    position_value    TEXT,
    fees_day          TEXT,
    fees_cum          TEXT,
    positions_json    TEXT,
    flags_json        TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, line_id, currency, date)
);

CREATE TABLE line_state (
    run_id            TEXT NOT NULL REFERENCES eval_run(run_id),
    line_id           TEXT NOT NULL REFERENCES strategy_line(line_id),
    currency          TEXT NOT NULL,
    date              TEXT NOT NULL,        -- 该日开盘前的状态
    state_json        TEXT NOT NULL,
    state_hash        TEXT NOT NULL,
    PRIMARY KEY (run_id, line_id, currency, date)
);

CREATE TABLE sim_fill (
    run_id            TEXT NOT NULL REFERENCES eval_run(run_id),
    line_id           TEXT NOT NULL REFERENCES strategy_line(line_id),
    date              TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    code              TEXT NOT NULL,
    side              TEXT NOT NULL,
    qty               TEXT NOT NULL,
    price             TEXT NOT NULL,
    fee               TEXT NOT NULL,
    bar_start         TEXT,
    ambiguous         INTEGER NOT NULL DEFAULT 0,
    note              TEXT,
    PRIMARY KEY (run_id, line_id, date, seq)
);

CREATE TABLE run_cost (
    run_id            TEXT NOT NULL REFERENCES eval_run(run_id),
    cost_kind         TEXT NOT NULL,        -- data | llm | infra ...
    currency          TEXT NOT NULL,
    amount            TEXT NOT NULL,
    note              TEXT,
    PRIMARY KEY (run_id, cost_kind, currency)
);

CREATE TRIGGER trg_sleeve_no_update BEFORE UPDATE ON sleeve_daily BEGIN SELECT RAISE(ABORT, 'sleeve_daily 冻结后只读'); END;
CREATE TRIGGER trg_sleeve_no_delete BEFORE DELETE ON sleeve_daily BEGIN SELECT RAISE(ABORT, 'sleeve_daily 冻结后只读'); END;
CREATE TRIGGER trg_line_state_no_update BEFORE UPDATE ON line_state BEGIN SELECT RAISE(ABORT, 'line_state 只追加'); END;
CREATE TRIGGER trg_line_state_no_delete BEFORE DELETE ON line_state BEGIN SELECT RAISE(ABORT, 'line_state 只追加'); END;
CREATE TRIGGER trg_sim_fill_no_update BEFORE UPDATE ON sim_fill BEGIN SELECT RAISE(ABORT, 'sim_fill 只追加'); END;
CREATE TRIGGER trg_sim_fill_no_delete BEFORE DELETE ON sim_fill BEGIN SELECT RAISE(ABORT, 'sim_fill 只追加'); END;
CREATE TRIGGER trg_batch_no_update BEFORE UPDATE ON comparison_batch BEGIN SELECT RAISE(ABORT, 'comparison_batch 只追加'); END;
CREATE TRIGGER trg_batch_no_delete BEFORE DELETE ON comparison_batch BEGIN SELECT RAISE(ABORT, 'comparison_batch 只追加'); END;
CREATE TRIGGER trg_eval_run_no_delete BEFORE DELETE ON eval_run BEGIN SELECT RAISE(ABORT, 'eval_run 不可删除'); END;
