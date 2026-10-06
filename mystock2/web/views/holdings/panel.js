MS.registerPanel("holdings", function (root, d) {
  var h = MS.h;
  var cols = [
    { key: "code", label: "标的" },
    { key: "role", label: "角色" },
    { key: "qty", label: "账本数量", num: true },
    { key: "price", label: "收盘价", num: true },
    { key: "market_value", label: "市值", num: true },
    { key: "weight", label: "占该币种持仓市值", num: true },
    { key: "broker_cost", label: "券商平均成本", num: true },
    { key: "diluted_cost", label: "摊薄成本", num: true },
    { key: "unrealized", label: "浮动盈亏", hint: "按本地移动平均成本", num: true }
  ];
  root.appendChild(MS.card("持仓", [MS.table(cols, d.rows, { empty: "当前没有持仓" }),
    MS.note("券商平均成本与摊薄成本来自快照（摊薄成本把已实现盈亏摊入，可为负）；浮动盈亏按账本成交算出的本地移动平均成本，含开账估算成本时标「估算」。表头可点击排序，右上角可筛选美股/港股。")]));
  if (d.warnings && d.warnings.length) root.appendChild(MS.card("提示", MS.notes(d.warnings.map(function (w) {
    return w === "no_opening" ? "未登记开账点：账本和式可能不完整" : w;
  }))));
});
