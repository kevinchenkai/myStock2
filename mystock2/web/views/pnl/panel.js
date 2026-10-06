MS.registerPanel("pnl", function (root, d, ctx) {
  var h = MS.h;
  d.summary.forEach(function (s) {
    var kids = [MS.kv([
      ["已实现盈亏 · 精确（费用后）", s.realized_exact],
      ["已实现盈亏 · 估算（费用后）", s.realized_estimated],
      ["无成本证据的卖出（盈亏不可用）", s.has_unavailable ? { text: s.unavailable_qty.text + " 股", tag: "不可用" } : s.unavailable_qty],
      ["费用合计（已计入盈亏）", s.fees_total],
      ["成交净现金流（不是盈亏）", s.trade_net_cashflow],
      ["开账日前成交（只作描述）", s.pre_opening + " 笔"]
    ], { stats: true })];
    root.appendChild(MS.card("币种 " + s.currency, kids));
  });
  if (!d.summary.length) root.appendChild(MS.card("盈亏", MS.note("没有成交，无盈亏可算。")));
  root.appendChild(MS.card("按标的", MS.table([
    { key: "code", label: "标的" }, { key: "realized_exact", label: "精确", num: true }, { key: "realized_estimated", label: "估算", num: true },
    { key: "unavailable_qty", label: "无成本证据卖出股数", num: true }, { key: "unavailable_net_proceeds", label: "其净收入（非盈亏）", num: true },
    { key: "fees_total", label: "费用合计", num: true }, { key: "buys", label: "买入笔数", num: true }, { key: "sells", label: "卖出笔数", num: true }
  ], d.by_code, { empty: "没有成交" })));
  root.appendChild(MS.card("逐笔卖出", MS.table([
    { key: "at", label: "时间", render: function (r) { return MS.fmtTime(r.at); } }, { key: "code", label: "标的" }, { key: "qty", label: "数量", num: true },
    { key: "price", label: "卖价", num: true }, { key: "avg_cost", label: "移动平均成本", num: true }, { key: "net_proceeds", label: "净收入", num: true },
    { key: "realized", label: "已实现盈亏", num: true }, { key: "quality_text", label: "口径" }
  ], d.sells, { empty: "没有卖出" })));
  if (d.pre_opening.length) {
    root.appendChild(MS.card("开账日前成交（只作描述）", [MS.table([
      { key: "at", label: "时间", render: function (r) { return MS.fmtTime(r.at); } }, { key: "code", label: "标的" }, { key: "side", label: "方向" },
      { key: "qty", label: "数量", num: true }, { key: "price", label: "价格", num: true }, { key: "pnl", label: "盈亏", num: true }
    ], d.pre_opening), MS.note("开账日前没有成本证据，这些成交不产生精确盈亏（不可用）。")]));
  }
  var f = d.finance;
  if (f) {
    var years = f.years.indexOf(f.year) >= 0 ? f.years : [f.year].concat(f.years);
    var chips = h("span", { class: "seg", role: "group", "aria-label": "财务统计年度" }, years.map(function (y) {
      var b = h("button", { type: "button", class: "seg-btn" + (y === f.year ? " on" : ""), text: y, "aria-pressed": y === f.year ? "true" : "false" });
      b.addEventListener("click", function () { if (ctx && ctx.go) ctx.go({ year: y }); });
      return b;
    }));
    var fkids = [h("div", { class: "tbl-toolbar" }, [h("span", { class: "muted small", text: "年度" }), chips])];
    if (!f.markets.length) fkids.push(MS.note(f.year + " 年度无成交记录。"));
    f.markets.forEach(function (m) {
      fkids.push(h("div", { class: "card" }, [h("h3", { text: m.market_text + " · " + m.currency }), MS.kv([
        ["净现金流（卖出额 − 买入额）", m.net_cashflow], ["卖出额", m.sell_amount], ["买入额", m.buy_amount],
        ["卖出 / 买入笔数", m.sell_count + " / " + m.buy_count], ["卖出 / 买入股数", m.sell_qty.text + " / " + m.buy_qty.text],
        ["费用合计", m.fees_total], ["含费用净现金流", m.net_after_fees]
      ])]));
    });
    fkids.push(MS.note(f.note));
    root.appendChild(MS.card("财务统计（年度现金流）", fkids));
  }
  var n = (d.warnings || []).map(function (w) { return w === "no_opening" ? "未登记开账点：全部成交按开账后处理，结果可能不完整" : w.indexOf("oversold:") === 0 ? "超卖：" + w.split(":")[1] + " 的卖出超过账本可追溯库存" : w; });
  root.appendChild(MS.card("口径说明", [MS.notes(["已实现盈亏＝移动平均成本法、费用后；不含股息、利息、汇兑。",
    "估算＝平均成本混有开账快照里的券商成本；不可用＝没有成本证据，不记零。",
    "成交净现金流是买入为负、卖出为正的现金流水，不是盈亏，不着色。"].concat(n))]));
});
