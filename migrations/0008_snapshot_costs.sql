-- 真实首跑发现：富途持仓的 cost_price 是「摊薄成本」（可为负），average_cost 才是平均成本。
-- cost_basis 保留为历史列（= 券商 cost_price，口径＝摊薄成本，不得当作买入成本用）；新增两列分别存两种口径。
-- 只有 average_cost 可作为「开账成本证据」；旧快照这两列为 NULL（口径未知，不使用）。
ALTER TABLE snapshot_position ADD COLUMN average_cost TEXT;
ALTER TABLE snapshot_position ADD COLUMN diluted_cost TEXT;
