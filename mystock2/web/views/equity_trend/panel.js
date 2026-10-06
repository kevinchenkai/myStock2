MS.registerPanel("equity_trend", function (root, d) {
  var h = MS.h;
  var COLORS = { market_value: { color: "var(--s1)", dash: "" }, equity: { color: "var(--s2)", dash: "" }, profit: { color: "var(--s3)", dash: "" } };
  function cssVar(name) {
    var m = /^var\((--[a-z0-9-]+)\)$/.exec(name);
    return m ? getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() || "#2b59d6" : name;
  }
  d.series.forEach(function (s) {
    var xs = s.points.map(function (p) { return p.date; });
    var gaps = {};
    s.points.forEach(function (p) { if (p.status === "gap") gaps[p.date] = "缺 " + p.missing.join("、") + " 的收盘价"; });
    var marks = s.flows.map(function (f) { return { x: f.date, label: "外部资金流 " + f.amount.text + "（只改变权益，不算收益）" }; });
    function line(k) { return { name: d.names[k], color: cssVar(COLORS[k].color), dash: COLORS[k].dash, points: s.points.map(function (p) { return { y: p[k] }; }) }; }
    var valueHost = h("div"), profitHost = h("div");
    // 持仓市值与账户权益同一量纲、共用一张图；「剔除外部资金流的收益」量级不同，单独一张图（不再被压在底部看不出起伏）
    var card = MS.card("币种 " + s.currency, [
      s.latest ? MS.kv([
        ["账户权益（" + s.latest.date + "）", s.latest.equity],
        ["持仓市值（" + s.latest.date + "）", s.latest.market_value],
        ["剔除外部资金流的收益（自 " + s.base_date + " 起）", s.latest.profit]
      ], { stats: true }) : MS.note("没有可估值的日子（全部为行情缺口）。"),
      s.latest_is_gap ? h("p", { class: "notice", text: "最近的交易日缺行情，当日不画点（上面是最近一个有行情的日子）。" }) : null,
      MS.legend(["equity", "market_value"].map(function (k) { return { name: d.names[k], color: cssVar(COLORS[k].color), dash: COLORS[k].dash }; })),
      valueHost,
      h("h3", { text: d.names.profit + "（自 " + s.base_date + " 起，入金/出金不计入）" }),
      profitHost
    ]);
    root.appendChild(card);
    MS.lineChart(valueHost, { xs: xs, ccy: s.currency, label: s.currency + " 账户权益与持仓市值", area: false,
      series: [line("equity"), line("market_value")], gaps: gaps, marks: marks });
    MS.lineChart(profitHost, { xs: xs, ccy: s.currency, label: s.currency + " " + d.names.profit, height: 170, area: false,
      series: [line("profit")], gaps: gaps, marks: marks });
    var more = [];
    if (s.gaps.length) more.push(h("p", null, "缺口（缺行情，不连线、不插值）：" + s.gaps.map(function (g) { return g.from + (g.to !== g.from ? "～" + g.to : "") + "（缺 " + g.missing.join("、") + "）"; }).join("；")));
    if (s.flows.length) more.push(h("p", null, "外部资金流（图中底部三角）：" + s.flows.map(function (f) { return f.date + " " + f.amount.text; }).join("；")));
    more.push(MS.note("显示最近 " + s.shown_points + " / " + s.total_points + " 个交易日；收益自基准日 " + s.base_date + " 起算，入金当天不显示为收益。"));
    card.appendChild(h("div", null, more));
  });
  var tips = [
    "三条曲线是不同的量：持仓市值 ≠ 账户权益 ≠ 剔除外部资金流的收益。",
    "权益估值用未复权收盘价；缺行情的日子标缺口，不插值、不记零。",
    d.snapshot_count ? "券商快照 " + d.snapshot_count + " 份（最近 " + MS.fmtTime(d.broker_snapshots[d.broker_snapshots.length - 1]) + "）。" : "没有券商快照，趋势完全来自账本与行情。"
  ];
  root.appendChild(MS.card("说明", MS.notes(tips)));
});
