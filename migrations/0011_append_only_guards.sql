-- 0011 只追加的纵深防御（审核 L-01）：开账点与待匹配队列在库层禁止改写／删除。
-- （`INSERT OR REPLACE` 的隐式删除绕过 DELETE 触发器的问题由写连接打开 recursive_triggers 解决，见 core/db.py。）
CREATE TRIGGER trg_account_opening_no_update BEFORE UPDATE ON account_opening BEGIN SELECT RAISE(ABORT, 'account_opening 不可改：开账点只能经更正事件调整'); END;
CREATE TRIGGER trg_account_opening_no_delete BEFORE DELETE ON account_opening BEGIN SELECT RAISE(ABORT, 'account_opening 不可删'); END;
CREATE TRIGGER trg_pending_match_no_update BEFORE UPDATE ON pending_match BEGIN SELECT RAISE(ABORT, 'pending_match 只追加：结清请写 pending_resolution'); END;
CREATE TRIGGER trg_pending_match_no_delete BEFORE DELETE ON pending_match BEGIN SELECT RAISE(ABORT, 'pending_match 只追加：结清请写 pending_resolution'); END;
