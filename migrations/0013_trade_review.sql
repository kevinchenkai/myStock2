-- 0013 单笔交易 AI 评价缓存（复盘卡里的「AI 评价」）。
-- 每次请求（含「刷新」）新增一行，不覆盖旧评价；页面取同一笔成交最新一行。status：running → ok / error。
-- 只存评价文本与回执，不存账户号；request_text 是实际发给模型的提示词（便于审计「发出去了什么」）。
CREATE TABLE trade_review (
    review_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id        TEXT NOT NULL,                 -- 账本里的成交 id（仅本机库内引用，不发给模型）
    code           TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash     TEXT NOT NULL,                 -- 发给模型的输入（不含时间戳）的哈希：输入变了（如 +20 日到期）可判断缓存已过时
    model          TEXT NOT NULL,
    effort         TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('running','ok','error')),
    request_text   TEXT NOT NULL,
    response_text  TEXT,
    error          TEXT,
    pid            INTEGER,                       -- 运行中的后台进程号（判断卡死用）
    requested_at   TEXT NOT NULL,
    finished_at    TEXT,
    duration_s     REAL
);
CREATE INDEX idx_trade_review_deal ON trade_review(deal_id, review_id);
CREATE TRIGGER trg_trade_review_no_delete BEFORE DELETE ON trade_review BEGIN SELECT RAISE(ABORT, 'trade_review 只追加（不删除）'); END;
