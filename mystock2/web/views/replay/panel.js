MS.registerPanel("replay", function (root, d) {
  var h = MS.h;
  function column(title, items, empty) {
    var body = items && items.length ? MS.notes(items) : MS.note(empty);
    return h("div", { class: "card" }, [h("h3", { text: title }), body]);
  }
  function showCard(r) {
    var x = r.detail, oc = x.outcome;
    var outItems = oc.horizons.map(function (o) { return "成交后 " + o.days + " 个交易日涨跌 · 复权：" + o.change.text; });
    outItems.push("持有窗口内最大不利变动：" + oc.max_adverse.text);
    outItems.push("持有窗口内最大有利变动：" + oc.max_favorable.text);
    outItems.push("库存：成交前 " + x.inventory.before.text + " → 成交后 " + x.inventory.after.text);
    var content = MS.card(null, [
      h("div", { class: "cols" }, [
        column("事实", x.facts, "无"),
        column("证据 · 当时已有", x.evidence, "无"),
        column("诊断 · 推测，非事实", x.diagnosis, "无"),
        column("结果", outItems, "无"),
        column("缺口", x.gaps, "无缺口")
      ]),
      MS.note("诊断与结果是事后口径；不定义「当时应成交的最优价」。")
    ]);
    MS.openDialog("复盘卡 · " + r.side_text + " " + MS.codeLabel(r.code) + " · " + r.date, content);       // 悬浮弹窗：Esc / 点背景 / 关闭按钮可关
  }

  var c = d.cards;
  var cols = [
    { key: "date", label: "日期" }, { key: "code", label: "标的" }, { key: "side_text", label: "方向" },
    { key: "qty", label: "数量", num: true }, { key: "price", label: "成交价", num: true }, { key: "fee", label: "费用", num: true },
    { key: "motive", label: "动机" }, { key: "range_position", label: "区间位置", hint: "执行质量：成交价在当日高低区间中的位置，越小越好；事后诊断", num: true },
    { key: "o1", label: "+1日", num: true, render: function (r) { return r.outcomes[0].change; } },
    { key: "o5", label: "+5日", num: true, render: function (r) { return r.outcomes[1].change; } },
    { key: "o20", label: "+20日", num: true, render: function (r) { return r.outcomes[2].change; } },
    { key: "open", label: "复盘卡", render: function (r) { return { text: "查看" }; } }
  ];
  var table = MS.table(cols, c.rows, { empty: "没有成交（开账日前的历史成交只作描述，不生成复盘卡）", onRowClick: showCard });   // 排序/筛选/翻页后点击仍打开对应行的复盘卡
  root.appendChild(MS.card(c.title + " · 共 " + c.total + " 笔" + (c.shown < c.total ? " · 显示最近 " + c.shown + " 笔" : "") + " · 最新在前", [
    MS.note("点击任意一行，在弹窗里查看该笔的复盘卡：事实、证据、诊断、结果、缺口。"), table, MS.note(c.note)]));

  var b = d.behavior;
  root.appendChild(MS.card(b.title, [
    MS.table([
      { key: "name", label: "指标" }, { key: "value", label: "值", num: true }, { key: "sample", label: "样本量", num: true }, { key: "definition", label: "口径" }
    ], b.rows, { empty: "没有可计算的指标" }),
    MS.note(b.note)
  ]));

  var r = d.rounds;
  var rk = [h("p", { class: "notice", text: "诊断口径：诊断回合不是账本收益，与「盈亏」视图的移动平均成本口径不同，不可相加或直接比较。" }),
    MS.table([
      { key: "tag", label: "类型", render: function (x) { return { text: x.tag, tag: "诊断" }; } },
      { key: "code", label: "标的" }, { key: "open_date", label: "开仓日" }, { key: "close_date", label: "平仓日" },
      { key: "qty", label: "数量", num: true }, { key: "cost_unit", label: "成本单价", num: true }, { key: "exit_unit", label: "卖出单价", num: true },
      { key: "fees", label: "分摊费用", num: true }, { key: "pnl", label: "诊断盈亏", hint: "费用后，诊断口径，不是账本收益", num: true },
      { key: "holding_days", label: "持有天数", num: true },
      { key: "flags", label: "缺口", render: function (x) { return x.flags.join("；") || "—"; } }
    ], r.rows, { empty: "没有已平仓回合" })];
  rk.push(MS.note(r.note));
  root.appendChild(MS.card(r.title, rk));
});
