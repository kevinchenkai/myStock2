MS.registerPanel("scoreboard", function (root, d) {
  var h = MS.h;
  var STYLE = {
    ai: { color: "var(--s1)", dash: "" }, ai_veto: { color: "var(--s2)", dash: "" }, ai_lgbm: { color: "var(--s4)", dash: "" },
    human_plan: { color: "var(--s3)", dash: "6 3" }, buyhold: { color: "var(--s5)", dash: "2 3" }, human_actual: { color: "var(--s3)", dash: "6 3" }
  };
  function cssVar(name) {
    var m = /^var\((--[a-z0-9-]+)\)$/.exec(name);
    return m ? getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() || "#2b59d6" : name;
  }
  function n(v) { return v === null || v === undefined ? { text: "不可用", na: true } : String(v); }

  root.appendChild(h("div", { class: "notice", role: "note" }, [h("strong", { text: "请注意　" }), d.banner]));

  var p = d.protocol;
  var pilot = p.pilot.value;
  var protoKids = [
    MS.kv([
      ["批次", d.batch_id], ["币种", d.currency], ["E0（共同起点权益）", d.e0], ["起始日", d.start_date],
      ["执行协议版本", p.exec_protocol.version], ["批次状态包哈希", p.batch_state_hash],
      ["记分牌 run", d.run ? d.run.run_id + "（" + MS.fmtTime(d.run.created_at) + "）" : { text: "不可用（没有 run）", na: true }],
      ["pilot 标记", { text: pilot ? "pilot（不是确认样本）" : "非 pilot", tag: pilot ? "pilot" : null }]
    ])
  ];
  if (pilot) protoKids.push(MS.notes(p.pilot.reasons));
  if (p.coach_protocols.length) {
    protoKids.push(MS.table([
      { key: "version", label: "教练协议版本" }, { key: "frozen", label: "冻结登记", render: function (r) { return r.frozen ? "已冻结" : { text: "未登记", tag: "pilot" }; } },
      { key: "hash", label: "协议哈希", render: function (r) { return r.hash ? r.hash.slice(0, 16) : { text: "不可用", na: true }; } },
      { key: "frozen_at", label: "冻结时间", render: function (r) { return r.frozen_at ? MS.fmtTime(r.frozen_at) : { text: "不可用", na: true }; } },
      { key: "tickets", label: "操作单数", num: true }
    ], p.coach_protocols));
  }
  if (d.runs.length > 1) protoKids.push(MS.note("该批次有 " + d.runs.length + " 个 run（旧 run 保留不覆盖）；用参数 run 切换：" + d.runs.map(function (r) { return r.run_id; }).join("、")));
  root.appendChild(MS.card("协议与批次", protoKids));

  var cols = [
    { key: "name", label: "线", render: function (r) { return { text: r.name, title: r.line_id }; } },
    { key: "latest_status_text", label: "最近状态", render: function (r) { return r.latest_status === "OK" || !r.latest_status ? r.latest_status_text : { text: r.latest_status_text, tag: r.latest_status }; } },
    { key: "r", label: "累计收益 R(T)", num: true, render: function (r) { return r.metrics.cumulative_return; } },
    { key: "mdd", label: "最大回撤", num: true, render: function (r) { return r.metrics.max_drawdown; } },
    { key: "to", label: "换手", num: true, render: function (r) { return r.metrics.turnover; } },
    { key: "ex", label: "平均敞口", num: true, render: function (r) { return r.metrics.avg_exposure; } },
    { key: "cov", label: "覆盖率", num: true, render: function (r) { return r.metrics.coverage; } },
    { key: "ok", label: "正常日 / 计划日", num: true, render: function (r) { return r.metrics.days_ok === null ? { text: "不可用", na: true } : r.metrics.days_ok + " / " + r.metrics.days_planned; } },
    { key: "unk", label: "未知日", num: true, render: function (r) { return n(r.metrics.unknown_days); } },
    { key: "pau", label: "暂停日", num: true, render: function (r) { return n(r.metrics.paused_days); } },
    { key: "amb", label: "歧义日", num: true, render: function (r) { return n(r.metrics.ambiguous_days); } },
    { key: "fills", label: "成交笔数", num: true, render: function (r) { return n(r.metrics.fills); } },
    { key: "fees", label: "累计费用", num: true, render: function (r) { return r.metrics.fees_cum; } }
  ];
  function drawChart(host, chart) {
    var xs = chart.dates;
    var series = chart.series.map(function (s) {
      var st = STYLE[s.kind] || STYLE.ai;
      return { name: s.name, color: cssVar(st.color), dash: st.dash, points: s.points.map(function (y) { return { y: y }; }) };
    });
    host.appendChild(MS.legend(series.map(function (s) { return { name: s.name, color: s.color, dash: s.dash }; })));
    var box = h("div");
    host.appendChild(box);
    MS.lineChart(box, { xs: xs, series: series, gaps: chart.gaps, ccy: chart.currency, label: "权益曲线 " + chart.currency, height: 260 });
    host.appendChild(MS.note(chart.note));
  }

  var later = [];                                  // 图表必须在卡片挂到页面之后再画（需要容器宽度）
  // ---- 区 1：正式模拟线
  var f = d.formal;
  var fk = [h("p", { class: "muted small", text: "同起点、同费用、同资金约束；权益 E 为交易仓口径、未复权收盘价；UNKNOWN/PAUSED 日不记零。" }),
            MS.table(cols, f.lines, { empty: "该批次没有正式模拟线" })];
  var fhost = h("div");
  fk.push(fhost);
  if (f.chart) later.push(function () { drawChart(fhost, f.chart); }); else fk.push(MS.note("没有可画的权益曲线（无 run 或无日度结果）：不可用。"));
  root.appendChild(MS.card("① " + f.title, fk));

  // ---- 配对差
  var prow = f.pairs.map(function (q) {
    if (!q.available) return { name: q.name, paired: { text: "不可用", na: true }, cov: q.text || { text: "不可用", na: true }, cum: { text: "不可用", na: true }, mean: { text: "不可用", na: true }, exa: { text: "不可用", na: true }, ci: { text: "不可用", na: true }, why: q.reason };
    var iv = q.interval;
    return {
      name: q.name, paired: q.paired_days + " / " + q.planned_days, cov: q.coverage, cum: q.cumulative, mean: q.mean,
      exa: { text: q.excluding_ambiguous.paired_days + " 日；ΣΔ " + q.excluding_ambiguous.cumulative.text },
      ci: iv.lo.na ? { text: "样本不足（n=" + iv.n + "）", na: true } : { text: iv.lo.text + " ～ " + iv.hi.text + "（n=" + iv.n + "）" }, why: q.note
    };
  });
  root.appendChild(MS.card("配对差 · 日度 · Δ=r_A−r_B · 分母固定为 E0", [
    MS.table([
      { key: "name", label: "对比" }, { key: "paired", label: "配对日 / 计划日", num: true }, { key: "cov", label: "配对覆盖率", num: true },
      { key: "cum", label: "累计差 ΣΔ", num: true }, { key: "mean", label: "日均差", num: true },
      { key: "exa", label: "剔除歧义日后", num: true }, { key: "ci", label: "块自助法区间", hint: "描述性", num: true }, { key: "why", label: "说明" }
    ], prow),
    MS.note("只在两条线同日均为 OK 的日期上配对；区间只作描述，不触发晋级。这里不给单一的胜负结论。")
  ]));

  // ---- 区 2：human_actual 描述性
  var ds = d.descriptive;
  var dk = [MS.note(ds.note)];
  if (ds.available) {
    dk.push(MS.table(cols, ds.lines, { empty: "没有 human_actual 线" }));
    var dhost = h("div");
    dk.push(dhost);
    if (ds.chart) later.push(function () { drawChart(dhost, ds.chart); }); else dk.push(MS.note("本 run 没有 human_actual 的日度结果：不可用。"));
  } else {
    dk.push(h("p", { class: "muted", text: "该批次没有登记 human_actual 线：不可用。" }));
  }
  root.appendChild(MS.card("② " + ds.title, dk));

  // ---- 区 3：live_guidance 占位
  root.appendChild(MS.card("③ live_guidance 真实账户指导单", [MS.kv([["状态", d.live_guidance.text]]), MS.note(d.live_guidance.note)]));
  if (d.warnings.length) root.appendChild(MS.card("提示", MS.notes(d.warnings)));
  later.forEach(function (fn) { fn(); });
});
