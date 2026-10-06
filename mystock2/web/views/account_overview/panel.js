MS.registerPanel("account_overview", function (root, d) {
  var h = MS.h;

  // 摘要：逐币种权益与现金（原币种，不相加）＋对账结论
  var rc0 = d.reconciliation, tiles = [];
  d.currencies.forEach(function (r) { tiles.push(["账户权益 · " + r.currency, r.equity]); tiles.push(["现金 · " + r.currency, r.cash]); });
  tiles.push(["对账（账本 vs 券商快照）", { text: rc0.label, tag: rc0.status === "ok" ? null : "需关注" }]);
  if (d.currencies.length) root.appendChild(MS.card(null, MS.kv(tiles, { stats: true })));

  // 逐币种（原币种；币种之间不相加）
  var cols = [
    { key: "currency", label: "币种" },
    { key: "cash", label: "现金", num: true },
    { key: "market_value", label: "持仓市值", num: true },
    { key: "receivable", label: "应收", num: true },
    { key: "equity", label: "账户权益", num: true },
    { key: "broker_cash", label: "券商快照现金", num: true }
  ];
  var rows = d.currencies.map(function (r) {
    var extra = [];
    if (r.unvalued && r.unvalued.length) extra.push("缺行情：" + r.unvalued.join("、"));
    if (r.stale_prices && r.stale_prices.length) extra.push("行情陈旧：" + r.stale_prices.join("、"));
    return Object.assign({}, r, { _extra: extra.join("；") });
  });
  var body = [MS.table(cols, rows, { empty: "账本里还没有现金或持仓" })];
  rows.forEach(function (r) {
    if (r._extra) body.push(h("p", { class: "small", text: r.currency + "：" + r._extra + (r.market_value_partial ? "（已估值部分 " + r.market_value_partial.text + "）" : "") }));
  });
  body.push(MS.note("权益 = 现金 + 持仓数量 × 未复权收盘价 + 应收；缺行情的标的使该币种市值与权益显示「不可用」，不记零。"));
  root.appendChild(MS.card("逐币种（原币种）", body));

  // 基准币种合计（选了才合计）
  if (d.base_ccy) {
    var t = d.total || {};
    var conv = MS.table([
      { key: "currency", label: "币种" },
      { key: "cash", label: "现金（折合 " + d.base_ccy + "）", num: true, render: function (r) { return r.converted ? r.converted.cash : { na: true, text: "不可用" }; } },
      { key: "mv", label: "持仓市值（折合）", num: true, render: function (r) { return r.converted ? r.converted.market_value : { na: true, text: "不可用" }; } },
      { key: "eq", label: "账户权益（折合）", num: true, render: function (r) { return r.converted ? r.converted.equity : { na: true, text: "不可用" }; } }
    ], d.currencies);
    var totalRow = MS.kv([["合计现金（" + d.base_ccy + "）", t.cash], ["合计持仓市值（" + d.base_ccy + "）", t.market_value], ["合计账户权益（" + d.base_ccy + "）", t.equity]]);
    var rate = MS.table([
      { key: "from", label: "从" }, { key: "to", label: "到" }, { key: "rate", label: "汇率", num: true }, { key: "path", label: "路径" },
      { key: "rate_date", label: "汇率日期" }, { key: "source", label: "来源" }
    ], d.rates, { empty: "没有换算" });
    var kids = [totalRow, conv, h("h3", { text: "汇率来源与时间" }), rate];
    if (t.unavailable && t.unavailable.length) kids.push(h("p", { class: "notice", text: "有币种缺汇率，合计显示「不可用」：" + t.unavailable.map(function (u) { return u.currency; }).join("、") }));
    root.appendChild(MS.card("基准币种合计（" + d.base_ccy + "）", kids));
  } else {
    root.appendChild(MS.card("基准币种合计", MS.note("未选基准币种：不合计（币种之间不直接相加）。在页头「基准币种」选择后，才会按有来源的汇率折算并合计。")));
  }

  // 持仓估值明细
  root.appendChild(MS.card("持仓估值明细（原币种，未复权收盘价）", MS.table([
    { key: "code", label: "标的" }, { key: "currency", label: "币种" }, { key: "qty", label: "数量", num: true },
    { key: "price", label: "收盘价", num: true }, { key: "market_value", label: "市值", num: true }
  ], d.positions, { empty: "当前没有持仓" })));

  // 对账
  var rc = d.reconciliation, kids = [];
  var kind = rc.status === "ok" ? "fresh-ok" : rc.status === "mismatch" ? "fresh-stale" : "fresh-unknown";
  kids.push(h("p", null, [MS.badge(rc.label, kind)]));
  if (rc.snapshot) kids.push(MS.note("快照 " + rc.snapshot.id + "（来源 " + rc.snapshot.source + "，时点 " + MS.fmtTime(rc.snapshot.captured_at) + "）"));
  if (rc.items && rc.items.length) kids.push(h("ul", { class: "plain" }, rc.items.map(function (i) { return h("li", { text: "［" + i.kind + "］" + i.text }); })));
  root.appendChild(MS.card("对账状态（账本 vs 券商快照）", kids));

  if (d.warnings && d.warnings.length) root.appendChild(MS.card("提示", MS.notes(d.warnings.map(function (w) {
    return w === "no_opening" ? "未登记开账点：账本和式可能不完整" : w.indexOf("pre_opening:") === 0 ? "有 " + w.split(":")[1] + " 笔开账日前事件，只作描述、不参与和式" : w.indexOf("fractional_position:") === 0 ? "碎股持仓：" + w.split(":")[1] : w;
  }))));
});
