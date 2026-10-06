MS.registerPanel("trades", function (root, d) {
  var h = MS.h;
  var cols = [
    { key: "event_at", label: "成交时间", render: function (r) { return { text: MS.fmtTime(r.event_at), tag: r.pre_opening ? "开账前·仅描述" : null }; } },
    { key: "code", label: "标的" },
    { key: "side", label: "方向" },
    { key: "qty", label: "数量", num: true },
    { key: "price", label: "成交价", num: true },
    { key: "notional", label: "成交额", num: true },
    { key: "fee", label: "费用（归属本笔）", num: true, render: function (r) { return r.fee_detail && r.fee_detail !== "未入账" ? { text: r.fee.text, v: r.fee.v, ccy: r.fee.ccy, title: r.fee_detail, tag: null } : r.fee; } },
    { key: "fee_detail", label: "费用构成" },
    { key: "net_cashflow", label: "成交净现金流", num: true },
    { key: "sources", label: "来源数", num: true },
    { key: "versions", label: "更正", render: function (r) { return r.corrected ? { text: "已更正（版本 " + r.versions + "）", tag: "更正" } : "—"; } }
  ];
  var head = [];
  head.push(MS.note("共 " + d.total + " 笔，显示 " + d.shown + " 笔（最新在前）" + (d.code_filter ? "；仅 " + d.code_filter : "") + (d.opening_at ? "；开账时点 " + MS.fmtTime(d.opening_at) : "；未登记开账点")));
  if (d.net_cashflow_totals && d.net_cashflow_totals.length) {
    root.appendChild(MS.card(null, [
      MS.kv([["成交笔数", String(d.total)]].concat(d.net_cashflow_totals.map(function (t) { return ["成交净现金流 · " + t.currency, t.amount]; })), { stats: true }),
      MS.note("成交净现金流：买入为负、卖出为正，含已入账费用；是现金流水，不是盈亏（买入未卖出只是现金变成了持仓）。盈亏见「盈亏」视图。")]));
  }
  root.appendChild(MS.card("成交流水", [head, MS.table(cols, d.rows, { empty: "没有成交" })]));
  if (d.unattributed_fees && d.unattributed_fees.length) {
    root.appendChild(MS.card("未归属的费用", [MS.table([{ key: "deal_id", label: "成交编号" }, { key: "kind", label: "类型" }, { key: "amount", label: "金额", num: true }], d.unattributed_fees),
      MS.note("这些费用事件指向的成交不在账本里（可能成交晚到），需在账本核对。")]));
  }
});
