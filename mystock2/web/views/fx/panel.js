MS.registerPanel("fx", function (root, d) {
  var h = MS.h;
  root.appendChild(MS.card("换算 · 美元 ⇄ 人民币", [MS.table([
    { key: "from", label: "从" }, { key: "to", label: "到" }, { key: "rate", label: "汇率", hint: "1 从 = 汇率 到", num: true },
    { key: "path", label: "路径" }, { key: "rate_date", label: "汇率日期", render: function (r) { return r.rate_date || "不可用"; } },
    { key: "source", label: "来源" }, { key: "needed", label: "账户需要", render: function (r) { return r.needed ? "是" : "—"; } }
  ], d.paths), MS.note("路径可以是直接币对、反向币对或经 USD 中转；缺任一段即「不可用」。汇率是比率，用中性色，不按红涨绿跌。")]));
  var pts = d.history.map(function (x) { return { y: x.rate }; });
  var host = h("div");
  var kids = [host];
  if (!d.history.length) kids.unshift(h("p", { class: "notice", text: "最近 " + d.history_days + " 天没有 " + d.pair + " 的汇率记录：不可用。" }));
  else {
    var color = getComputedStyle(document.documentElement).getPropertyValue("--fx").trim() || "#55688f";
    var gaps = {};                                      // 缺口（>4 天）的区间在图上标灰带（审核 W-P3）
    d.gaps.forEach(function (g) { gaps[g.to] = g.from + " → " + g.to + " 无汇率（" + g.days + " 天）"; });
    MS.lineChart(host, { xs: d.history.map(function (x) { return x.date; }), ccy: d.pair.slice(3), fmt: function (y) { return MS.fmtDec(y, 4) + " " + d.pair.slice(3) + "/" + d.pair.slice(0, 3); }, series: [{ name: d.pair + " 汇率", color: color, points: pts }], label: d.pair + " 汇率历史", gaps: gaps, height: 220 });
    kids.push(MS.table([{ key: "date", label: "日期" }, { key: "rate", label: "汇率", num: true, render: function (r) { return { text: MS.fmtDec(r.rate, 6).replace(/0+$/, "").replace(/\.$/, ""), fx: true, v: r.rate }; } },
      { key: "source", label: "来源" }, { key: "inverse", label: "取自", render: function (r) { return r.inverse ? "反向币对" : "原币对"; } }], d.history.slice(-15).reverse()));
  }
  if (d.gaps.length) kids.push(h("p", null, "汇率历史缺口（>4 天）：" + d.gaps.map(function (g) { return g.from + " → " + g.to + "（" + g.days + " 天）"; }).join("；")));
  root.appendChild(MS.card(d.pair + " 汇率历史（近 " + d.history_days + " 天）", kids));
  root.appendChild(MS.card("库中已有的币对", MS.table([{ key: "pair", label: "币对" }, { key: "latest_date", label: "最新日期" }, { key: "received_at", label: "收到时间", render: function (r) { return MS.fmtTime(r.received_at); } }], d.stored_pairs, { empty: "库里没有任何汇率" })));
});
