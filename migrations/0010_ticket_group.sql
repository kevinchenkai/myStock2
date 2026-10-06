-- 0010 冻结组（审核 P0-3）：一次冻结写入的整组操作单的显式成员清单。
-- 选择规则以「截止前最后一个冻结组」为单元（§6A.3）；内容未变、沿用旧行的单（no-op）也登记为新组成员，
-- 否则否决只改一张买单时，其余单（含卖单）会因不在最新组而失效。只追加，不可改、不可删。
CREATE TABLE ticket_group (
    group_id       TEXT PRIMARY KEY,
    batch_id       TEXT NOT NULL,
    line_id        TEXT NOT NULL,
    kind           TEXT NOT NULL,
    market         TEXT NOT NULL,
    target_session TEXT NOT NULL,
    stage          TEXT NOT NULL,
    frozen_at      TEXT NOT NULL,                 -- 本组的冻结时刻（= 新写入单的 visible_at）
    member_ids     TEXT NOT NULL                  -- JSON 数组：本组全部 ticket_id（含沿用的旧行）
);
CREATE INDEX idx_ticket_group_cell ON ticket_group(batch_id, line_id, kind, market, target_session, frozen_at);
CREATE TRIGGER trg_ticket_group_no_update BEFORE UPDATE ON ticket_group BEGIN SELECT RAISE(ABORT, 'ticket_group 只追加'); END;
CREATE TRIGGER trg_ticket_group_no_delete BEFORE DELETE ON ticket_group BEGIN SELECT RAISE(ABORT, 'ticket_group 只追加'); END;
