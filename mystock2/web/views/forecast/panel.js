MS.registerPanel("forecast", function (root, d, ctx) {
  var h = MS.h;
  var COLORS = { baseline: "var(--s4)", lgbm: "var(--s5)" };
  var DASH = { baseline: "", lgbm: "5 3" };
  var RING = { baseline: 5, lgbm: 3 };
  function cssVar(name) {
    var m = /^var\((--[a-z0-9-]+)\)$/.exec(name);
    return m ? getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() || "#888888" : name;
  }
  var NA = { text: "不可用", na: true };
  function mdl(role) { return d.models.filter(function (m) { return m.role === role; })[0]; }
  function tagInfo(tag) { return d.provenance.filter(function (p) { return p.tag === tag; })[0]; }

  // ---- 固定文案与来源
  root.appendChild(h("div", { class: "state", role: "note" }, [h("strong", { text: "请注意　" }), d.banner]));
  var cur = tagInfo(d.source);
  var span = cur.first_as_of ? cur.first_as_of + " ～ " + cur.last_as_of : NA;
  root.appendChild(MS.card("样本来源与留档", [
    MS.kv([
      ["当前统计的来源", { text: d.source_label, tag: d.source === "rebuilt" ? "不是前向证据" : null }],
      ["前向（forward）样本数", String(d.forward_count)],
      ["事后重建（rebuilt）样本数", String(d.rebuilt_count)],
      ["当前来源的预测起止（as_of）", span],
      ["最新预测 as_of", d.freshness.latest_prediction ? d.freshness.latest_prediction.as_of : NA],
      ["最新预测 generated_at", d.freshness.latest_prediction ? MS.fmtTime(d.freshness.latest_prediction.generated_at) : NA],
      ["最新行情日", d.freshness.latest_quote_date || NA]
    ]),
    MS.table([
      { key: "label", label: "来源" }, { key: "count", label: "预测条数（两模型合计）", num: true },
      { key: "codes", label: "标的数", num: true },
      { key: "first_as_of", label: "最早 as_of", render: function (r) { return r.first_as_of || NA; } },
      { key: "last_as_of", label: "最晚 as_of", render: function (r) { return r.last_as_of || NA; } }
    ], d.provenance),
    MS.table([
      { key: "label", label: "模型" }, { key: "version", label: "版本" },
      { key: "predictions", label: "预测条数", num: true }, { key: "scored", label: "已有实际值", num: true }, { key: "pending", label: "目标日未结束", num: true },
      { key: "target_low", label: "名义目标：低点跌破", num: true }, { key: "target_high", label: "名义目标：高点突破", num: true }
    ], d.models),
    MS.notes(d.warnings)
  ]));

  // ---- 模型对比
  var bm = mdl("baseline"), lm = mdl("lgbm");
  function tgt(m, side) { var c = m[side]; return c && c.na ? "不可用" : c.text; }
  var cmp = d.compare;
  function mcol(role, name, key, label) {
    return { key: role + key, label: name + "·" + label, num: true, render: function (r) { return r[role][key]; } };
  }
  var cols = [
    { key: "code", label: "标的" },
    { key: "n", label: "共同样本", num: true, render: function (r) { return r.enough ? String(r.n) : { text: "不足（" + r.n + "）", na: true, title: "样本 < " + d.min_n }; } },
    mcol("baseline", "基线", "cover_low", "跌破低点"),
    mcol("baseline", "基线", "cover_high", "突破高点"),
    mcol("baseline", "基线", "width", "宽度"),
    mcol("baseline", "基线", "pinball", "pinball"),
    mcol("lgbm", "LGBM", "cover_low", "跌破低点"),
    mcol("lgbm", "LGBM", "cover_high", "突破高点"),
    mcol("lgbm", "LGBM", "width", "宽度"),
    mcol("lgbm", "LGBM", "pinball", "pinball"),
    { key: "improvement", label: "LGBM 改善", num: true, render: function (r) { return r.improvement; } }
  ];
  var targets = "名义目标（尾部覆盖率）：基线 低点跌破 " + tgt(bm, "target_low") + "、高点突破 " + tgt(bm, "target_high") +
    "；LGBM 低点跌破 " + tgt(lm, "target_low") + "、高点突破 " + tgt(lm, "target_high") + "。";
  var rows = cmp.rows.concat([cmp.summary]);
  var gate = cmp.gate;
  root.appendChild(MS.card("模型对比（" + d.source_label + "，描述性）", [
    h("p", { class: "muted small", text: "对次日日内最低/最高价区间的预测 vs 实际。跌破低点＝实际日内低点低于预测低点；突破高点＝实际日内高点高于预测高点（越接近名义目标越好）。平均宽度相对 T 日收盘价；pinball 为低侧＋高侧损失的样本均值（占收盘价比例，越小越好）；只在两个模型都有预测且目标日已有终值日线的共同样本上比较；样本 < " + d.min_n + " 显示「不足」。" }),
    MS.note(targets),
    MS.table(cols, rows, { empty: "该来源下没有预测样本：不可用" }),
    MS.note("宽度＝平均区间宽度（相对 T 日收盘价）；pinball＝raw 总损失；改善＝(基线 − LGBM) / 基线，正数表示 LGBM 损失更小。"),
    h("p", null, [h("strong", { text: "V1 门槛的数值判定（仅描述）　" }), gate.text]),
    gate.excluded.length ? MS.note("未纳入判定的标的（样本不足或缺一个模型）：" + gate.excluded.join("、")) : null,
    MS.note(gate.disclaimer + " 汇总行：覆盖率、宽度、pinball 按样本合并；改善为各标的改善的等权平均（门槛口径）。")
  ]));

  // ---- 图
  var kids = [];
  if (d.chart && d.chart.dates.length) {
    var ch = d.chart;
    var models = ch.models.map(function (m) {
      return { name: m.name, role: m.role, color: cssVar(COLORS[m.role]), dash: DASH[m.role], ring: RING[m.role], points: m.points };
    });
    kids.push(h("p", { class: "muted small", text: d.symbol + "　最近 " + ch.dates.length + " 个交易日（窗口 " + d.window + "；价位单位 " + ch.currency + "）" }));
    kids.push(MS.legend(models.map(function (m) { return { name: m.name + "预测区间", color: m.color, dash: m.dash }; })));
    var box = h("div");
    kids.push(box);
    kids.push(MS.note(ch.note));
    var nB = models.map(function (m) { return m.points.filter(function (p) { return p && p.breach_low; }).length + " / " + m.points.filter(function (p) { return p && p.breach_high; }).length; });
    kids.push(MS.note("窗口内突破次数（跌破低点 / 突破高点）：" + models.map(function (m, i) { return m.name + " " + nB[i]; }).join("；")));
    root.appendChild(MS.card("预测带 vs 实际日内高低（" + d.symbol + "）", kids));
    MS.bandChart(box, { dates: ch.dates, actual: ch.actual, models: models, ccy: ch.currency, label: d.symbol + " 预测带图", height: 300 });
  } else {
    root.appendChild(MS.card("预测带 vs 实际日内高低", h("p", { class: "muted", text: "该来源下没有可画的预测或行情：不可用。" })));
  }

  // ---- 最新预测
  function rangeCell(role) {
    return function (r) {
      if (r.sealed) return { text: "已密封", na: true };
      var m = r.models[role], lo = m.low, hi = m.high;
      if (lo.na || hi.na) return lo.na ? lo : hi;
      var ccy = lo.text.slice(lo.text.lastIndexOf(" ") + 1);
      return { text: lo.text.slice(0, lo.text.lastIndexOf(" ")) + " – " + hi.text, v: lo.v, subs: [{ text: lo.rel.text, dir: lo.rel.dir }, { text: hi.rel.text, dir: hi.rel.dir }], title: "预测低点 – 高点；下方为相对基准收盘价的位置（" + ccy + "）" };
    };
  }
  var lcols = [
    { key: "code", label: "标的", render: function (r) { return { text: r.code, tag: (r.kind === "latest" ? "最新预测" : "最近已结算") + " · " + r.sources.join("、") }; } },
    { key: "status", label: "状态", render: function (r) { return { text: r.status, title: r.status_title || null, tag: r.sealed ? "密封" : null }; } },
    { key: "as_of", label: "as_of → 目标日", render: function (r) { var t = r.as_of + " → " + r.target.slice(5); return r.pred_lag ? { text: t, tag: "预测落后于行情" } : r.lag ? { text: t, tag: "行情落后" } : t; } },
    { key: "base", label: "基准收盘价", num: true, render: function (r) { return r.base_close.na ? r.base_close : { text: r.base_close.text, v: r.base_close.v, subs: r.base_date ? [{ text: r.base_date }] : null }; } },
    { key: "b", label: "基线 预测低 – 高（相对基准）", num: true, render: rangeCell("baseline") },
    { key: "l", label: "LGBM 预测低 – 高（相对基准）", num: true, render: rangeCell("lgbm") },
    { key: "act", label: "实际低 ／ 高（目标日）", num: true, render: function (r) { return r.actual ? { text: r.actual.low.text.replace(/ [A-Z]+$/, "") + " ／ " + r.actual.high.text, v: r.actual.low.v } : { text: r.sealed ? "未发生" : "不可用", na: true }; } }
  ];
  root.appendChild(MS.card("各标的最新预测", [
    h("p", { class: "muted small", text: "每个标的取最新 as_of 的预测。目标日尚无终值日线、且没有揭示记录时，价位与区间一律不显示（已密封，见实施方案 §6A.2）；其下另列最近一次目标日已结束的预测供对照。基准收盘价：最新预测用最新行情日收盘价，最近已结算用当时（as_of）收盘价；缺失显示「不可用」。" }),
    MS.table(lcols, d.latest, { empty: "没有预测：不可用" })
  ]));
});
