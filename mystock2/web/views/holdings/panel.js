MS.registerPanel("holdings", function (root, d) {
  var h = MS.h;
  var cols = [
    { key: "code", label: "标的" },
    { key: "role", label: "角色" },
    { key: "qty", label: "账本数量", num: true },
    { key: "broker_qty", label: "券商快照数量", num: true, render: function (r) {
      return r.qty_match === null ? r.broker_qty : { text: r.broker_qty.text + (r.qty_match ? "（一致）" : "（不一致）"), tag: r.qty_match ? null : "未对账" };
    } },
    { key: "price", label: "收盘价", num: true },
    { key: "market_value", label: "市值", num: true },
    { key: "weight", label: "占该币种持仓市值", num: true },
    { key: "broker_cost", label: "券商成本（快照）", num: true },
    { key: "diluted_cost", label: "摊薄成本", num: true },
    { key: "local_cost", label: "本地移动平均成本", num: true },
    { key: "unrealized", label: "浮动盈亏（按本地成本）", num: true },
    { key: "order", label: "当前操作单" }
  ];
  root.appendChild(MS.card("持仓（原币种）", [MS.table(cols, d.rows, { empty: "当前没有持仓" }),
    MS.note("三类成本并列、互不覆盖：券商成本来自快照原值；摊薄成本快照没有则「不可用」；本地移动平均成本由账本成交算出，含开账估算成本时标「估算」。")]));
  if (d.warnings && d.warnings.length) root.appendChild(MS.card("提示", MS.notes(d.warnings.map(function (w) {
    return w === "no_opening" ? "未登记开账点：账本和式可能不完整" : w;
  }))));
});
