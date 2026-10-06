MS.registerPanel("data_status", function (root, d) {
  var h = MS.h;
  function t(v) { return v === null || v === undefined || v === "" ? { text: "不可用", na: true } : String(v); }
  function stateCell(r) { return { text: r.state_text, tag: r.problem ? "需关注" : null }; }

  var s = d.summary, pv = d.protocols.verdict;
  root.appendChild(MS.card("总览", [
    MS.kv([
      ["采集有问题 / 总数（标的×种类×来源）", { text: s.collection_problems + " / " + s.collection_groups, tag: s.collection_problems ? "需关注" : null }],
      ["行情有问题的标的 / 总数", { text: s.quote_problems + " / " + s.quote_codes, tag: s.quote_problems ? "需关注" : null }],
      ["最近运行回执有问题", { text: String(s.run_problems), tag: s.run_problems ? "需关注" : null }],
      ["预测版本总数", String(s.prediction_versions)],
      ["协议冻结", { text: pv.text, tag: pv.pilot ? "pilot" : null }]
    ], { stats: true }),
    d.calendar_warnings.length ? MS.notes(d.calendar_warnings.map(function (w) { return "交易日历提示：" + w.status + "，剩余 " + w.calendar_days_left + " 天（覆盖至 " + w.calendar_end + "）"; })) : null
  ]));

  root.appendChild(MS.card("采集回执", [
    MS.table([
      { key: "code", label: "标的/币对" }, { key: "kind_text", label: "种类" }, { key: "source", label: "来源" },
      { key: "state_text", label: "状态", render: stateCell },
      { key: "last_ok_at", label: "最近成功", render: function (r) { return r.last_ok_at ? MS.fmtTime(r.last_ok_at) : { text: "从未成功", na: true }; } },
      { key: "last_attempt_at", label: "最近尝试", render: function (r) { return MS.fmtTime(r.last_attempt_at) + "（" + r.last_status + "）"; } },
      { key: "counts", label: "累计 成功/空/失败/陈旧/部分", render: function (r) { var c = r.counts; return c.ok + "/" + c.empty + "/" + c.error + "/" + c.stale + "/" + c.partial; } },
      { key: "last_detail", label: "最近一次问题详情", render: function (r) { return r.last_detail || "—"; } }
    ], d.collection, { empty: "没有采集回执：不可用" }),
    MS.note("失败、空结果、陈旧一律保留并显示，不记零；超过设定小时数没有成功采集标「陈旧」。")
  ]));

  root.appendChild(MS.card("行情", [
    MS.table([
      { key: "code", label: "标的" },
      { key: "state_text", label: "状态", render: stateCell },
      { key: "daily", label: "最新日线", render: function (r) { return r.latest_daily ? r.latest_daily.session_date + "（" + r.latest_daily.quality + "）" : { text: "不可用", na: true }; } },
      { key: "expected_session", label: "应有的最近收盘日", render: function (r) { return t(r.expected_session); } },
      { key: "missing", label: "区间内缺口交易日", render: function (r) {
        if (r.missing_count === null) return { text: "不可用", na: true };
        return r.missing_count ? r.missing_count + " 个：" + r.missing_sessions.join("、") + (r.missing_count > r.missing_sessions.length ? " …" : "") : "无";
      } },
      { key: "non_ok_days", label: "质量非 ok 日数", num: true, render: function (r) { return r.non_ok_days === null ? { text: "不可用", na: true } : String(r.non_ok_days); } },
      { key: "hourly", label: "最新小时线", render: function (r) { return r.latest_hourly ? MS.fmtTime(r.latest_hourly.bar_start) + (r.latest_hourly.complete ? "" : "（未走完）") : { text: "不可用", na: true }; } },
      { key: "hp", label: "小时线归档问题日", num: true, render: function (r) { return r.hourly_problem_days === null ? { text: "不可用", na: true } : String(r.hourly_problem_days); } }
    ], d.quotes, { empty: "没有行情：不可用" }),
    MS.note("缺口按交易所日历计算（区间起点不早于该标的最早行情日），缺失不填充、不记零。")
  ]));

  if (d.fx.length) {
    root.appendChild(MS.card("汇率", MS.table([
      { key: "pair", label: "币对" }, { key: "latest_rate_date", label: "最新汇率日" }, { key: "age_days", label: "距今天数", num: true },
      { key: "source", label: "来源" }, { key: "received_at", label: "收到时间", render: function (r) { return MS.fmtTime(r.received_at); } }
    ], d.fx)));
  }

  var p = d.predictions;
  root.appendChild(MS.card("预测版本", [
    MS.kv([["版本总数", String(p.total)], ["最新目标日", t(p.latest_target_session)], ["最近生成", p.latest_generated_at ? MS.fmtTime(p.latest_generated_at) : { text: "不可用", na: true }]]),
    MS.table([{ key: "model_version", label: "模型版本" }, { key: "source_tag", label: "来源标签" }, { key: "count", label: "版本数", num: true },
      { key: "latest_target_session", label: "最新目标日" }], p.by_model, { empty: "没有预测版本" }),
    MS.table([{ key: "code", label: "标的" }, { key: "count", label: "版本数", num: true }, { key: "latest_target_session", label: "最新目标日" },
      { key: "latest_generated_at", label: "最近生成", render: function (r) { return MS.fmtTime(r.latest_generated_at); } }], p.by_code),
    MS.note(p.note)
  ]));

  root.appendChild(MS.card("最近运行回执", [
    MS.table([
      { key: "command", label: "命令" }, { key: "status", label: "状态", render: function (r) { return { text: r.status, tag: r.problem ? "需关注" : null }; } },
      { key: "started_at", label: "开始", render: function (r) { return MS.fmtTime(r.started_at); } },
      { key: "finished_at", label: "结束", render: function (r) { return r.finished_at ? MS.fmtTime(r.finished_at) : { text: "未结束", na: true }; } },
      { key: "retry_scope", label: "可重试范围", render: function (r) { return r.retry_scope || "—"; } },
      { key: "detail", label: "摘要", render: function (r) { var k = Object.keys(r.detail); return k.length ? k.map(function (x) { return x + "=" + r.detail[x]; }).join("；") : "—"; } },
      { key: "run_id", label: "run_id" }
    ], d.runs.recent, { empty: "没有运行回执：不可用" })
  ]));

  root.appendChild(MS.card("账户快照", MS.table([
    { key: "account_id", label: "账户" }, { key: "snapshots", label: "快照数", num: true },
    { key: "captured_at", label: "最近快照", render: function (r) { return r.captured_at ? MS.fmtTime(r.captured_at) : { text: "不可用", na: true }; } },
    { key: "age", label: "距今" }, { key: "source", label: "来源", render: function (r) { return t(r.source); } },
    { key: "positions", label: "持仓行数", num: true, render: function (r) { return r.positions === null ? { text: "不可用", na: true } : String(r.positions); } }
  ], d.snapshots, { empty: "没有账户" })));

  var pr = d.protocols;
  root.appendChild(MS.card("协议冻结", [
    h("p", null, [MS.badge(pr.verdict.pilot ? "pilot" : "非 pilot", pr.verdict.pilot ? "fresh-stale" : "fresh-ok"), " " + pr.verdict.text]),
    MS.table([
      { key: "protocol_version", label: "协议版本" }, { key: "pilot", label: "pilot", render: function (r) { return r.pilot ? { text: "是", tag: "pilot" } : "否"; } },
      { key: "frozen_at", label: "冻结时间", render: function (r) { return MS.fmtTime(r.frozen_at); } },
      { key: "hash", label: "协议哈希", render: function (r) { return r.hash.slice(0, 16); } },
      { key: "code_sha", label: "代码提交", render: function (r) { return t(r.code_sha); } },
      { key: "missing", label: "缺失项", render: function (r) { return r.missing.join("、") || "无"; } }
    ], pr.rows, { empty: "没有冻结登记" })
  ]));
});
