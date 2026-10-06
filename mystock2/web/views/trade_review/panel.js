/* 复盘卡「AI 评价」：读缓存、没有就请求（后台 Codex）、可刷新。
   只显示服务端给的文本；Markdown 只支持 ## 标题、- 列表、**粗体**，全部用 DOM 节点渲染（不拼 HTML 字符串）。 */
(function () {
  var h = MS.h;
  var POLL_MS = 3000, START_GRACE_MS = 45000;

  function inline(parent, text) {
    String(text).split(/(\*\*[^*]+\*\*)/).forEach(function (part) {
      if (!part) return;
      if (part.length > 4 && part.slice(0, 2) === "**" && part.slice(-2) === "**") parent.appendChild(h("strong", { text: part.slice(2, -2) }));
      else parent.appendChild(document.createTextNode(part));
    });
  }
  function renderMarkdown(text) {
    var root = h("div", { class: "ai-md" }), list = null;
    String(text || "").split("\n").forEach(function (raw) {
      var line = raw.replace(/\s+$/, "");
      if (!line.trim()) { list = null; return; }
      var m = /^#{1,4}\s+(.*)$/.exec(line);
      if (m) { list = null; root.appendChild(h("h4", { class: "ai-h", text: m[1] })); return; }
      m = /^\s*[-*]\s+(.*)$/.exec(line);
      if (m) {
        if (!list) { list = h("ul", { class: "ai-list" }); root.appendChild(list); }
        var li = h("li"); inline(li, m[1]); list.appendChild(li); return;
      }
      list = null;
      var p = h("p", { class: /评分[:：]/.test(line) ? "ai-scores" : "ai-p" });
      inline(p, line); root.appendChild(p);
    });
    return root;
  }

  MS.reviewWidget = function (host, dealId) {
    var timer = null, postedAt = null;
    function alive() { return document.body && document.body.contains(host); }
    function later() { clearTimeout(timer); timer = setTimeout(load, POLL_MS); }
    function load() {
      if (!alive()) return;
      MS.getJSON("/api/v/trade_review?deal_id=" + encodeURIComponent(dealId)).then(paint, function (e) { showError("无法读取评价：" + (e && e.message || e)); });
    }
    function request(refresh) {
      postedAt = Date.now();
      paintBusy("正在请求本机 Codex…");
      MS.postJSON("/api/review/deal", { deal_id: dealId, refresh: !!refresh }).then(function (r) {
        if (r._http >= 400) { postedAt = null; showError((r.error && r.error.message) || "请求失败"); return; }
        load();
      }, function (e) { postedAt = null; showError("请求失败：" + (e && e.message || e)); });
    }
    function btn(label, fn, primary) {
      var b = h("button", { type: "button", class: "btn" + (primary ? " primary" : ""), text: label });
      b.addEventListener("click", fn);
      return b;
    }
    function paintBusy(text, since) {
      host.textContent = "";
      host.appendChild(h("p", { class: "ai-busy", text: text + (since ? "（已 " + Math.max(0, Math.round((Date.now() - since) / 1000)) + " 秒，通常 30–90 秒）" : "") }));
      host.appendChild(MS.loading());
    }
    function showError(text, retry) {
      host.textContent = "";
      host.appendChild(h("p", { class: "notice", text: text }));
      if (retry !== false) host.appendChild(btn("重试", function () { request(true); }));
    }
    function paint(payload) {
      if (!alive()) return;
      if (payload.status !== "ok") { showError((payload.error && payload.error.message) || "评价不可用", false); return; }
      var d = payload.data, r = d.review;
      if (d.state === "running") {
        paintBusy("Codex 正在评价这一笔", d.running_since ? Date.parse(d.running_since) : postedAt);
        later();
        return;
      }
      if (d.state === "none") {
        if (postedAt && Date.now() - postedAt < START_GRACE_MS) { paintBusy("正在启动后台请求", postedAt); later(); return; }
        if (postedAt) { postedAt = null; showError("请求没有启动（详见 data/logs/review.log）"); return; }
        request(false);                                               // 第一次打开：没有缓存就请求一次，之后读缓存
        return;
      }
      postedAt = null;
      host.textContent = "";
      if (d.error && !r) { showError("评价失败：" + d.error); return; }
      if (d.error) host.appendChild(h("p", { class: "notice", text: "最近一次刷新失败：" + d.error + "（下面仍是上一次成功的评价）" }));
      if (d.stale) host.appendChild(h("p", { class: "notice", text: "输入已变化（例如 +5/+20 日结果刚到期），可点「刷新」按最新数据重新评价。" }));
      host.appendChild(renderMarkdown(r.text));
      var meta = [r.model + " · " + r.effort, MS.fmtTime(r.finished_at), r.duration_s ? "用时 " + Math.round(r.duration_s) + " 秒" : null, "已缓存", d.history_count > 1 ? "共 " + d.history_count + " 次评价" : null].filter(Boolean).join(" · ");
      host.appendChild(h("div", { class: "ai-foot" }, [h("span", { class: "muted small", text: meta }), btn("刷新评价", function () { request(true); })]));
      host.appendChild(MS.note("仅作复盘参考，不是投资建议；只发送这一笔的脱敏摘要（不含账户号、成交号、现金与总权益）。"));
    }
    paintBusy("正在读取评价缓存");
    load();
  };

  MS.registerPanel("trade_review", function (root, d) {
    var box = h("div");
    root.appendChild(MS.card("AI 评价", box));
    MS.reviewWidget(box, d.deal_id);
  });
})();
