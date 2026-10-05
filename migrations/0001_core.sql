-- 0001 core：运行回执与标的主数据（实施方案 §6：run_log、instrument 属 M1）。
-- schema_migration 由迁移器自身创建。迁移只前进：不得修改已应用的迁移文件，需新增迁移。

CREATE TABLE run_log (
    run_id        TEXT PRIMARY KEY,
    command       TEXT NOT NULL,
    inputs_json   TEXT NOT NULL DEFAULT '{}',   -- 输入引用（不含密钥与账号）
    started_at    TEXT NOT NULL,                -- UTC ISO-8601
    finished_at   TEXT,
    status        TEXT NOT NULL CHECK (status IN ('running','ok','failed','partial')),
    detail_json   TEXT NOT NULL DEFAULT '{}',
    retry_scope   TEXT                          -- 失败时可重试的范围描述
);

CREATE TABLE instrument (
    code          TEXT PRIMARY KEY,             -- 富途标准代码，如 US.NVDA / HK.00700
    market        TEXT NOT NULL CHECK (market IN ('HK','US')),
    yf_symbol     TEXT NOT NULL,
    currency      TEXT NOT NULL,
    name          TEXT,
    valid_from    TEXT,
    valid_to      TEXT
);
