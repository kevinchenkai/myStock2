/* 股票详情面板：弹窗（MS.openStock）与 #/stock?code=… 页面共用。每个块独立：来源缺失只让该块显示「不可用」。
   金额与数量一律用服务器生成的单元（带币种、缺失为 na），这里不做任何金额运算。 */
MS.registerPanel("stock", function (root, d, ctx) {
  var h = MS.h;
  function cssVar(name) {
    var m = /^var\((--[a-z0-9-]+)\)$/.exec(name);
    return m ? getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() || "#2b59d6" : name;
  }
  function unavailable(b) { return h("p", { class: "muted", text: "不可用：" + ((b && b.reason) || "来源缺失") }); }
  function section(title, b, build) {
    if (!b || b.status !== "ok") { root.appendChild(MS.card(title, unavailable(b))); return; }
    var out = build(b), after = null;
    if (out && out.__after) { after = out.__after; out = out.kids; }     // 图表须先挂载再绘制（按容器宽度取尺寸）
    root.appendChild(MS.card(title, out));
    if (after) after();
  }
  function na(reason) { return { text: "不可用", na: true, title: reason || null }; }

  // ① 抬头
  var hd = d.head || {};
  var headKids = [];
  if (!(ctx && ctx.modal)) headKids.push(h("h2", { text: MS.codeLabel(d.code) }));
  headKids.push(MS.kv([
    ["代码", d.code], ["中文名", hd.name || na()], ["英文名", hd.long_name || na()],
    ["行业", hd.sector || na()], ["细分行业", hd.industry || na()], ["交易所", hd.exchange || na()],
    ["币种", hd.currency || na()]
  ]));
  if (hd.profile_currency && hd.currency && hd.profile_currency.text !== hd.currency.text) headKids.push(MS.note("提示：档案登记的币种（" + hd.profile_currency.text + "）与代码推断的币种不同，展示金额按代码推断的币种（" + hd.currency.text + "）。"));
  root.appendChild(MS.card("抬头", headKids));

  // ② 行情
  var q = d.quote, r52 = d.range52;
  var qKids = [];
  if (!q || q.status !== "ok") qKids.push(unavailable(q));
  else {
    qKids.push(MS.kv([
      ["最新收盘价（未复权）", q.close], ["日涨跌", q.change], ["日涨跌幅", q.change_pct], ["前一交易日收盘", q.prev_close],
      ["收盘日", q.session_date + (q.stale ? "（陈旧：应有 " + q.expected_session + "）" : "")],
      ["当日最低 – 最高", q.day_range ? q.day_range.low.text + " – " + q.day_range.high.text : na()]
    ]));
    if (q.stale) qKids.push(h("p", { class: "notice", text: "行情陈旧：最新有行情的收盘日（" + q.session_date + "）早于应有的最近收盘日（" + q.expected_session + "）。" }));
  }
  if (r52 && r52.status === "ok") {
    var rows = [];
    var c = r52.calc, p = r52.profile;
    rows.push({ kind: c ? "自算：近 " + c.days + " 个交易日日线（未复权）" : "自算：日线", high: c ? { text: c.high.text, v: c.high.v, ccy: c.high.ccy, title: "出现在 " + c.high_date } : na(r52.reason),
      low: c ? { text: c.low.text, v: c.low.v, ccy: c.low.ccy, title: "出现在 " + c.low_date } : na(r52.reason),
      note: c ? c.from + " 至 " + c.to + "；最高出现在 " + c.high_date + "，最低出现在 " + c.low_date + (c.partial ? "（不足 250 个交易日）" : "") + (c.stale ? "（行情陈旧）" : "") : (r52.reason || "没有日线行情") });
    rows.push({ kind: "档案来源值", high: p.high, low: p.low, note: p.source ? "来源 " + p.source + "；as_of " + (p.as_of ? MS.fmtTime(p.as_of) : "未知") : "没有档案" });
    qKids.push(h("h3", { text: "52 周高低" }));
    qKids.push(MS.table([{ key: "kind", label: "口径" }, { key: "high", label: "最高", num: true }, { key: "low", label: "最低", num: true }, { key: "note", label: "说明" }], rows, { sortable: false }));
    qKids.push(MS.note("两个口径并列显示、互不覆盖：自算值来自本地日线的最高/最低价（未复权，遇拆股会与档案不同）；档案值是来源给出的原值，时点见 as_of。"));
  } else {
    qKids.push(h("h3", { text: "52 周高低" }));
    qKids.push(unavailable(r52));
  }
  root.appendChild(MS.card("行情", qKids));

  // ③ 持仓
  section("持仓", d.position, function (b) {
    var kids = [];
    if (!b.held) { kids.push(h("p", { class: "muted", text: b.message || "当前未持有" })); return kids; }
    kids.push(MS.kv([
      ["账本数量", b.qty],
      ["券商快照数量", b.qty_match === null ? b.broker_qty : { text: b.broker_qty.text + (b.qty_match ? (b.changed_since_snapshot ? "（快照时一致，之后有成交）" : "（一致）") : "（不一致）"), tag: b.qty_match ? null : "未对账" }],
      ["市值", b.market_value], ["占该币种持仓市值", b.weight],
      ["券商平均成本", b.broker_cost], ["摊薄成本", b.diluted_cost], ["本地移动平均成本", b.local_cost], ["浮动盈亏（按本地成本）", b.unrealized]
    ]));
    kids.push(MS.note("账户 " + b.account_id + (b.snapshot ? "；快照时点 " + MS.fmtTime(b.snapshot.captured_at) : "；没有快照") +
      "。三类成本并列、互不覆盖：券商平均成本与摊薄成本来自快照（摊薄成本把已实现盈亏摊入，可为负）；本地移动平均成本由账本成交算出，含开账估算成本时标「估算」。成本不可用时浮动盈亏显示「不可用」。"));
    return kids;
  });

  // ④ 价格走势
  section("价格走势（近 250 个交易日收盘，未复权）", d.chart, function (b) {
    var host = h("div");
    var legend = h("div", { class: "legend" }, [
      h("span", null, [h("span", { class: "mk mk-buy", text: "▲ 买入" }), "　"]),
      h("span", null, [h("span", { class: "mk mk-sell", text: "▼ 卖出" }), "　（落在当日成交均价处；同日同向合并）"])
    ]);
    var kids = [legend, host];
    var notes = ["区间 " + b.from + " 至 " + b.to + "；收盘价为未复权价，缺行情的交易日断开、不插值。点按/悬停图表可看当日数值与买卖说明。"];
    if (b.unplaced_fills) notes.push(b.unplaced_fills + " 笔成交的日期在图的范围之外或当日缺行情，未标注。");
    if (b.mark_note) notes.push(b.mark_note);
    kids.push(MS.notes(notes));
    return { __after: function () {
      MS.lineChart(host, {
        xs: b.dates, ccy: b.ccy, label: d.code + " 收盘价走势（买卖标记）", height: 260, fmt: function (v) { return MS.fmtPx(v) + " " + b.ccy; },
        series: [{ name: "收盘价", color: cssVar("var(--s2)"), dash: "", points: b.closes.map(function (y) { return { y: y }; }) }],
        gaps: b.gaps, marks: b.marks
      });
    }, kids: kids };
  });

  // ⑤ 成交与已实现盈亏、股息
  var t = d.trades;
  var tKids = [];
  if (!t || t.status !== "ok") tKids.push(unavailable(t));
  else {
    if (t.summary) {
      var s = t.summary;
      tKids.push(MS.kv([
        ["已实现盈亏 · 精确（费用后）", s.realized_exact], ["已实现盈亏 · 估算（费用后）", s.realized_estimated],
        ["无成本证据的卖出股数", s.has_unavailable ? { text: s.unavailable_qty.text + " 股", tag: "不可用" } : s.unavailable_qty],
        ["费用合计（已计入盈亏）", s.fees_total], ["买入 / 卖出笔数", s.buys + " / " + s.sells]
      ]));
    } else tKids.push(MS.note("账本里没有该标的的成交或开账持仓，无盈亏可算。"));
    tKids.push(MS.note("共 " + t.total + " 笔，显示最近 " + t.shown + " 笔（最新在前）。已实现盈亏沿用「盈亏」视图口径：移动平均成本、费用后，不含股息/利息/汇兑；精确＝成本全来自开账后的买入，估算＝混有开账快照成本，不可用＝没有成本证据（不记零）；开账日及以前的成交只作描述。「成交净现金流」是现金流水，不是盈亏。"));
    tKids.push(MS.table([
      { key: "event_at", label: "成交时间", render: function (x) { return { text: MS.fmtTime(x.event_at), tag: x.pre_opening ? "开账前·仅描述" : null }; } },
      { key: "side", label: "方向" }, { key: "qty", label: "数量", num: true }, { key: "price", label: "成交价", num: true },
      { key: "notional", label: "成交额", num: true }, { key: "fee", label: "费用", num: true },
      { key: "net_cashflow", label: "成交净现金流", num: true },
      { key: "realized", label: "已实现盈亏", num: true }, { key: "quality_text", label: "口径" }
    ], t.rows, { empty: "没有该标的的成交", market: false }));
    (t.warnings || []).forEach(function (w) { if (w === "no_opening") tKids.push(MS.note("未登记开账点：全部成交按开账后处理，结果可能不完整。")); });
  }
  root.appendChild(MS.card("最近成交与已实现盈亏", tKids));

  section("股息与预扣税（账本 DIVIDEND_PAYMENT / TAX 事件）", d.dividends, function (b) {
    var kids = [];
    if (!b.count) kids.push(h("p", { class: "muted", text: "账本里没有该标的的股息记录（无记录，不是 0）。" }));
    else {
      b.totals.forEach(function (x) {
        kids.push(MS.kv([["累计股息现金入账（" + x.cash.ccy + "）", x.cash], ["累计预扣税", x.tax], ["性质未知的差额（只知净额）", x.shortfall], ["到手净额（入账 − 预扣税）", x.net]]));
      });
      kids.push(MS.table([
        { key: "event_at", label: "支付时间", render: function (x) { return MS.fmtTime(x.event_at); } }, { key: "cash", label: "现金入账", num: true },
        { key: "tax", label: "预扣税", num: true }, { key: "shortfall", label: "性质未知的差额", num: true }, { key: "net", label: "到手净额", num: true }
      ], b.rows, { sortable: false }));
      kids.push(MS.note("共 " + b.count + " 笔，显示最近 " + b.rows.length + " 笔。预扣税按同一支付组归属；只知净额的股息，其差额性质（税/费/汇差）未知，单列、不记为税。"));
    }
    if (b.skipped_pre_opening) kids.push(MS.note(b.skipped_pre_opening + " 笔开账日及以前的股息记录只作描述，未计入。"));
    return kids;
  });

  // ⑥ 订单
  section("最近订单（意图，不是成交）", d.orders, function (b) {
    var kids = [];
    if (!b.total) { kids.push(h("p", { class: "muted", text: "没有该标的的订单记录。" })); return kids; }
    kids.push(MS.note("订单是「意图」，成交才是事实：订单（含已撤、失败）只用于复盘与行为分析，不进入持仓与盈亏；成交以账本为准。共 " + b.total + " 条，显示最近 " + b.shown + " 条。"));
    kids.push(h("p", { class: "small", text: "状态分布：" + b.by_status.map(function (x) { return x.status + " " + x.count; }).join("　") }));
    kids.push(MS.table([
      { key: "created_at", label: "下单时间", render: function (x) { return { text: MS.fmtTime(x.created_at), tag: x.assumed_tz ? "时区推断" : null }; } },
      { key: "side", label: "方向" }, { key: "order_type", label: "类型" },
      { key: "status", label: "状态", render: function (x) { return { text: x.status.text, title: x.status.raw }; } },
      { key: "price", label: "委托价", num: true }, { key: "qty", label: "委托数量", num: true }, { key: "dealt_qty", label: "已成交数量", num: true },
      { key: "dealt_avg_price", label: "成交均价", num: true }, { key: "source", label: "来源" }
    ], b.rows, { market: false }));
    return kids;
  });

  // ⑦ 资金流向
  section("资金流向（最近 20 个交易日）", d.flows, function (b) {
    var kids = [];
    if (b.stale) kids.push(h("p", { class: "notice", text: "资金流向陈旧：最新记录日 " + b.latest + " 早于应有的最近收盘日 " + (b.expected_session || "未知") + "。" }));
    kids.push(MS.table([
      { key: "date", label: "日期" }, { key: "in_flow", label: "净流入合计", num: true }, { key: "main_in_flow", label: "主力净流入", num: true },
      { key: "super_in_flow", label: "超大单", num: true }, { key: "big_in_flow", label: "大单", num: true }, { key: "mid_in_flow", label: "中单", num: true },
      { key: "sml_in_flow", label: "小单", num: true }, { key: "source", label: "来源" }
    ], b.rows, { market: false }));
    b.totals.forEach(function (x) {
      kids.push(MS.kv([["来源 " + x.source + " · 主力净流入合计（" + x.main_days + " / " + x.days + " 日有数据）", x.main_in_flow], ["来源 " + x.source + " · 净流入合计（" + x.in_days + " / " + x.days + " 日有数据）", x.in_flow]]));
    });
    kids.push(MS.note("单位：来源货币金额，币种 " + b.ccy + "（由代码推断）；净流入为正标红、为负标绿（红涨绿跌）；缺失的字段显示「不可用」，合计只含有数据的日子。不同来源分行列出、不相加。"));
    return kids;
  });

  // ⑧ 预测（只显示事后重建）
  section("预测区间（事后重建，非前向证据）", d.forecast, function (b) {
    var kids = [h("p", { class: "notice", text: "事后重建（rebuilt）：用历史数据在事后重新生成，不是前向、也不是当时可得的预测，不能当作晋级证据；预测区间不是成交保证，不构成投资建议。这里不显示任何前向 AI 操作单。" })];
    kids.push(MS.table([
      { key: "model", label: "模型", render: function (x) { return { text: x.model, title: x.model_version }; } },
      { key: "as_of", label: "数据截至" }, { key: "target", label: "目标日" },
      { key: "range", label: "预测区间", num: true, render: function (x) {
        return { text: x.low.text + " – " + x.high.text, subs: [{ text: "相对 T 日收盘 " + x.rel_low.text, dir: x.rel_low.dir }, { text: x.rel_high.text, dir: x.rel_high.dir }] };
      } },
      { key: "actual", label: "目标日实际", num: true, render: function (x) { return x.actual ? x.actual.low.text + " – " + x.actual.high.text : { text: "未结算", na: true }; } },
      { key: "generated_at", label: "重建时刻", render: function (x) { return MS.fmtTime(x.generated_at); } },
      { key: "tag", label: "来源标签" }
    ], b.rows, { sortable: false }));
    kids.push(MS.note("每个模型只取最新一条事后重建（as_of 最大者）；共 " + b.total_rebuilt + " 条重建记录。"));
    return kids;
  });

  // ⑨ 档案指标
  section("档案指标", d.profile, function (b) {
    var kids = [MS.kv(b.items.map(function (x) { return [x.label, x.cell]; }))];
    if (b.website) kids.push(h("p", { class: "small", text: "网站：" + b.website }));
    kids.push(MS.note("来源 " + (b.source || "未知") + "；as_of " + (b.as_of ? MS.fmtTime(b.as_of) : "未知") + "；入库 " + MS.fmtTime(b.updated_at) +
      "。描述性参考数据，数值按来源原值展示" + (b.ccy_inferred ? "；档案没有币种，市值/每股收益的币种按代码推断（" + b.ccy + "）" : "（币种 " + b.ccy + "）") + "；缺失显示「不可用」。"));
    return kids;
  });
});
