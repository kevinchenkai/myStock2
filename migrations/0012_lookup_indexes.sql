-- 0012 查询索引（审核 W-04／P3）：复盘页与复盘卡按（标的、目标日）逐张查操作单与人类计划；数据状态页按标的取采集回执。
-- 原先这些查询是全表扫描，比较批次开始后单据增长会让复盘页变慢。只加索引，不改数据。
CREATE INDEX idx_ticket_code_target ON ticket(code, target_session);
CREATE INDEX idx_intent_code_target ON intent(code, target_session);
CREATE INDEX idx_collection_log_code ON collection_log(code, kind, attempted_at);
