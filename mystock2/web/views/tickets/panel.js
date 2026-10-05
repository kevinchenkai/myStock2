MS.registerPanel("tickets", function (root, d) {
  var h = MS.h;
  var o = d.orders;
  if (o) {
    var okv = [["订单总数", String(o.total)]].concat(o.by_status.map(function (x) { return [x.text, String(x.count)]; }));
    root.appendChild(MS.card("我的订单（券商，实际操作；含已撤/失败）", [
      MS.kv(okv),
      MS.table([
        { key: "created_at", label: "下单时间", render: function (r) { return { text: MS.fmtTime(r.created_at), title: r.time_trust === "assumed_local_tz" ? "交易所本地时间按市场补时区（推断）" : null }; } },
        { key: "code", label: "标的" }, { key: "side_text", label: "方向" }, { key: "order_type", label: "类型" },
        { key: "status_text", label: "状态" }, { key: "price", label: "委托价", num: true }, { key: "qty", label: "委托数量", num: true },
        { key: "dealt_qty", label: "已成交数量", num: true }, { key: "dealt_avg_price", label: "成交均价", num: true },
        { key: "source", label: "来源" }
      ], o.rows, { empty: "没有订单记录" }),
      MS.note(o.note + (o.shown < o.total ? "（只显示最近 " + o.shown + " 条）" : ""))
    ]));
  }
  if (d.batch_id === null) {
    root.appendChild(MS.card("AI 操作单", [h("p", { class: "state", text: "暂无" }), MS.note(d.no_batch_text), MS.note(d.live_guidance.note)]));
    return;
  }
  root.appendChild(MS.card("批次 " + d.batch_id, [
    MS.kv([["批次起点", d.batch_start], ["币种", d.batch_currency], ["AI 线", (d.ai_lines || []).join("、") || "无"]]),
    MS.note("密封规则：未揭示的目标日只显示「已密封」与计数；揭示只能经命令行（coach show、人工否决包导出）写入暴露日志，网页不提供揭示入口。人类线与 live_guidance 不在此显示。")
  ]));
  if (!d.markets.length) root.appendChild(MS.card("操作单", MS.note("该批次还没有 AI 单。")));
  d.markets.forEach(function (m) {
    var sealed = m.state === "sealed";
    var title = "市场 " + m.market + " · 目标日 " + m.target_session;
    var body = [h("p", null, [MS.badge(m.state_text, sealed ? "fresh-unknown" : "fresh-ok"), " 截止 " + MS.fmtTime(m.deadline_at)])];
    var c = m.counts, cov = c.coverage;
    body.push(MS.kv([
      ["张数（全部版本）", String(c.tickets)], ["单元数（线×标的）", String(c.cells)],
      ["已冻结", String(c.by_status.frozen)], ["错过截止", String(c.by_status.missed_deadline)], ["不可用（缺关键数据）", String(c.by_status.unavailable)],
      ["覆盖：有单的单元 / 应有", cov.cells_with_ticket + " / " + (cov.planned_cells === null ? "未知" : cov.planned_cells)]
    ]));
    body.push(MS.note("阶段：" + (Object.keys(c.by_stage).map(function (k) { return k + " " + c.by_stage[k]; }).join("；") || "无")));
    if (sealed) {
      body.push(h("p", { class: "state", text: "已密封：该目标日的动作、限价、数量与原因在揭示之前不显示（API 同样不返回）。" }));
    } else {
      body.push(MS.note("揭示记录 " + m.reveal.count + " 条，首次 " + MS.fmtTime(m.reveal.first_revealed_at) + "，通道：" + m.reveal.channels.join("、")));
      body.push(MS.table([
        { key: "line_kind", label: "线" }, { key: "code", label: "标的" }, { key: "stage", label: "阶段" },
        { key: "action", label: "动作" }, { key: "limit_price", label: "限价", num: true }, { key: "qty", label: "数量", num: true },
        { key: "reserved_cash", label: "预留资金", num: true },
        { key: "reasons", label: "原因码", render: function (r) { return r.reasons.join("、") || "—"; } },
        { key: "effective", label: "状态", render: function (r) { return { text: r.effective.text, tag: r.status === "frozen" ? null : r.status_text }; } },
        { key: "deadline_at", label: "截止", render: function (r) { return MS.fmtTime(r.deadline_at); } },
        { key: "state_ref", label: "状态哈希（线内状态）" },
        { key: "frozen_hash", label: "单据哈希", render: function (r) { return r.frozen_hash.slice(0, 16); } },
        { key: "version_count", label: "版本数", num: true }
      ], m.rows, { empty: "该目标日没有 AI 单" }));
      var vrows = [];
      m.rows.forEach(function (r) { r.versions.forEach(function (v) { vrows.push({ line: r.line_kind, code: r.code, v: v }); }); });
      body.push(h("details", { class: "src" }, [h("summary", { text: "版本历史（" + vrows.length + "）：只采用截止前最后一个已冻结且可见的版本" }),
        MS.table([
          { key: "code", label: "标的", render: function (r) { return r.line + " " + r.code; } },
          { key: "vis", label: "可见时间", render: function (r) { return MS.fmtTime(r.v.visible_at); } },
          { key: "stage", label: "阶段", render: function (r) { return r.v.stage; } },
          { key: "st", label: "状态", render: function (r) { return r.v.status_text; } },
          { key: "act", label: "动作", render: function (r) { return r.v.action_text; } },
          { key: "px", label: "限价", render: function (r) { return r.v.limit_price; } },
          { key: "q", label: "数量", render: function (r) { return r.v.qty; } },
          { key: "pick", label: "采用", render: function (r) { return r.v.selected ? { text: "采用", tag: "唯一选择规则" } : r.v.after_deadline ? { text: "否", tag: "截止后" } : "否"; } }
        ], vrows)]));
    }
    root.appendChild(MS.card(title, body));
  });
  root.appendChild(MS.card("live_guidance（真实账户指导单）", [MS.kv([["状态", d.live_guidance.text]]), MS.note(d.live_guidance.note)]));
});
