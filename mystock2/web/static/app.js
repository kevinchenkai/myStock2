/* 外壳：导航、参数栏、新鲜度头部、状态与错误展示、面板加载。单个视图失败只影响该面板，不会让整页崩。 */
(function () {
  "use strict";
  var h = MS.h;
  var state = { views: [], byId: {} };
  var $ = function (id) { return document.getElementById(id); };

  function parseHash() {
    var raw = (location.hash || "").replace(/^#\/?/, ""), q = raw.indexOf("?");
    var id = decodeURIComponent(q >= 0 ? raw.slice(0, q) : raw), params = {};
    if (q >= 0) new URLSearchParams(raw.slice(q + 1)).forEach(function (v, k) { params[k] = v; });
    return { id: id, params: params };
  }
  function go(id, params) {
    var qs = new URLSearchParams();
    Object.keys(params || {}).forEach(function (k) { if (params[k] !== "" && params[k] !== null && params[k] !== undefined) qs.set(k, params[k]); });
    location.hash = "#/" + encodeURIComponent(id) + (qs.toString() ? "?" + qs.toString() : "");
  }

  function getJSON(url) {
    return fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" }).then(function (r) {
      return r.json().catch(function () { return { status: "error", error: { code: "bad_response", message: "服务器返回了无法解析的内容（HTTP " + r.status + "）" } }; })
        .then(function (j) { j._http = r.status; return j; });
    });
  }

  /* 导航按 view.yaml 的 group 分组（账户 / 教练 / 系统 / 其他），组的顺序＝组内第一个视图的顺序；窄屏单行横向滚动。 */
  function renderNav(current) {
    var nav = $("nav");
    nav.textContent = "";
    var groups = [], byName = {};
    state.views.filter(function (v) { return !v.hidden; }).forEach(function (v) {
      var g = v.group || "其他";
      if (!byName[g]) { byName[g] = h("div", { class: "nav-group", role: "group", "aria-label": g }, h("span", { class: "nav-label", text: g })); groups.push(byName[g]); }
      var a = h("a", { href: "#/" + encodeURIComponent(v.id), text: v.title, "aria-current": v.id === current ? "page" : null, title: v.description || null });
      byName[g].appendChild(a);
    });
    groups.forEach(function (g) { nav.appendChild(g); });
    var cur = nav.querySelector ? nav.querySelector('a[aria-current="page"]') : null;
    if (cur && cur.scrollIntoView && nav.scrollWidth > nav.clientWidth) { try { cur.scrollIntoView({ block: "nearest", inline: "center" }); } catch (e) { /* 旧浏览器 */ } }
  }
  function shortLabel(p) {                                          // 参数用中文短标签（description 的第一段），完整说明放 title
    var d = p.description || "";
    var s = d.split("（")[0].split("，")[0].trim();
    return s && s.length <= 14 ? s : (d ? d.slice(0, 12) : p.name);
  }

  function renderParams(view, payload, params) {
    var box = $("params");
    box.textContent = "";
    var data = payload && payload.data;
    var applied = (payload && payload.params) || {};
    function field(name, label, control, title) { return h("label", { title: title || null }, [label, control]); }
    (view.params || []).forEach(function (p) {
      var cur = params[p.name] !== undefined ? params[p.name] : (applied[p.name] !== undefined ? applied[p.name] : p.default);
      var ctl;
      if (p.name === "account") return;                                    // 账户选择器见下
      if (p.name === "symbol" && data && data.codes) {   // 标的选择器：选项来自视图数据（不是配置里的固定列表）
        var ssel = h("select", { "aria-label": "symbol" }, [h("option", { value: "", text: "（自动）" })].concat(data.codes.map(function (c) {
          return h("option", { value: c, selected: c === data.symbol ? true : null, text: MS.codeLabel(c) });
        })));
        ssel.addEventListener("change", function () { var np = Object.assign({}, params); np.symbol = ssel.value; go(view.id, np); });
        box.appendChild(field("symbol", "标的", ssel, "symbol"));
        return;
      }
      if (p.name === "symbol") return;                                     // 数据到达前不显示（选项来自数据）
      if (p.choices) {
        ctl = h("select", { "aria-label": p.name }, p.choices.map(function (c) {
          return h("option", { value: c, selected: String(c) === String(cur) ? true : null, text: c === "" ? "（无）" : String(c) });
        }));
      } else {
        ctl = h("input", { value: cur === null || cur === undefined ? "" : cur, type: p.type === "int" ? "number" : "text", size: 10, "aria-label": p.name });
      }
      ctl.addEventListener("change", function () { var np = Object.assign({}, params); np[p.name] = ctl.value; go(view.id, np); });
      box.appendChild(field(p.name, shortLabel(p), ctl, p.name + (p.description ? "：" + p.description : "")));
    });
    if (data && data.accounts && data.accounts.length > 1) {
      var sel = h("select", { "aria-label": "account" }, data.accounts.map(function (a) { return h("option", { value: a, selected: a === data.account_id ? true : null, text: a }); }));
      sel.addEventListener("change", function () { var np = Object.assign({}, params); np.account = sel.value; go(view.id, np); });
      box.appendChild(field("account", "账户", sel));
    }
  }

  /* 新鲜度：页头下一行的徽标＋一句话（新鲜/陈旧/未知＋采集时间）；数据模式、事件/采集时间与各来源收进可展开的「数据来源」；
     口径说明（header.notes）始终可见（小字），不藏起来——它们是诚实披露。 */
  function freshCard(header) {
    var s = header.staleness || {}, kind = s.label === "新鲜" ? "fresh-ok" : s.label === "陈旧" ? "fresh-stale" : "fresh-unknown";
    var grid = h("div", { class: "fresh" }, [
      h("div", null, [h("div", { class: "k", text: "数据模式" }), h("div", { class: "v", text: header.data_mode_label || "未知" })]),
      h("div", null, [h("div", { class: "k", text: "事件时间" }), h("div", { class: "v", text: MS.fmtTime(header.event_at) })]),
      h("div", null, [h("div", { class: "k", text: "采集时间" }), h("div", { class: "v", text: MS.fmtTime(header.collected_at) })]),
      h("div", null, [h("div", { class: "k", text: "陈旧度" }), h("div", { class: "v" }, MS.badge(s.label || "未知", kind))]),
    ]);
    var panel = [grid, h("p", { class: "muted small", text: s.text || "" })];
    if (header.sources && header.sources.length) {
      panel.push(MS.table([{ key: "name", label: "来源" }, { key: "event_at", label: "事件时间", render: function (r) { return MS.fmtTime(r.event_at); } },
        { key: "collected_at", label: "采集时间", render: function (r) { return MS.fmtTime(r.collected_at); } }, { key: "text", label: "陈旧度" }], header.sources,
        { market: false, sortable: false, paginate: false }));
    }
    var strip = h("div", { class: "fresh-strip" }, [
      MS.badge(s.label || "未知", kind),
      h("span", { text: (header.data_mode_label || "数据") + " · 采集 " + MS.fmtTime(header.collected_at) }),
      h("span", { class: "sep", text: "|" }),
      h("details", { class: "src" }, [h("summary", { text: "数据来源与时间" + (header.sources && header.sources.length ? "（" + header.sources.length + "）" : "") }),
        h("div", { class: "src-panel" }, panel)])
    ]);
    var notes = header.notes && header.notes.length ? h("ul", { class: "head-notes" }, header.notes.map(function (t) { return h("li", { text: t }); })) : null;
    return h("div", { class: "fresh-wrap" }, [strip, notes]);
  }
  function renderFresh(header) {
    var box = $("fresh");
    box.textContent = "";
    if (!header) return;
    box.appendChild(freshCard(header));
  }

  var loaded = {};
  function loadPanel(id) {
    if (MS.getPanel(id)) return Promise.resolve();
    if (loaded[id]) return loaded[id];
    loaded[id] = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = "/views/" + encodeURIComponent(id) + "/panel.js";
      s.onload = resolve; s.onerror = function () { reject(new Error("面板脚本加载失败")); };
      document.head.appendChild(s);
    });
    return loaded[id];
  }

  /* 状态：empty＝正常的「还没有数据」（如尚无比较批次）；unavailable＝缺数据；error＝出错。三者样式不同。 */
  var EMPTY_CODES = ["no_batch", "no_predictions", "no_account", "no_events", "no_prediction", "no_flow", "no_profile"];
  function showState(root, kind, title, text, code) {
    if (kind === "unavailable" && EMPTY_CODES.indexOf(code) >= 0) kind = "empty";
    var cls = kind === "error" ? "state error" : kind === "empty" ? "state empty" : "state";
    root.appendChild(h("div", { class: cls }, [h("strong", { text: kind === "empty" ? "暂无数据" : title }), h("div", { text: text }), code ? h("div", { class: "small", text: "状态码：" + code }) : null]));
  }

  var seq = 0;
  function render() {
    var route = parseHash(), my = ++seq;
    if (!route.id && state.views.length) { var first = state.views.filter(function (v) { return !v.hidden; })[0] || state.views[0]; return go(first.id, {}); }
    var view = state.byId[route.id], root = $("panel");
    renderNav(route.id);
    root.textContent = ""; $("fresh").textContent = ""; $("params").textContent = "";
    if (!view) { $("page-title").textContent = "没有这个视图"; $("page-desc").textContent = ""; showState(root, "unavailable", "没有这个视图", route.id || "（空）", "unknown_view"); return; }
    document.title = view.title + " · myStock2";
    $("page-title").textContent = view.title;
    $("page-desc").textContent = view.description || "";
    renderParams(view, null, route.params);
    root.appendChild(MS.loading());
    var qs = new URLSearchParams(route.params).toString();
    getJSON("/api/v/" + encodeURIComponent(view.id) + (qs ? "?" + qs : "")).then(function (payload) {
      if (my !== seq) return;
      root.textContent = "";
      if (payload.data && payload.data.code_names) MS.setNames(payload.data.code_names);
      renderParams(view, payload, route.params);
      renderFresh(payload.header || null);
      if (payload.status === "ok") {
        if (!view.has_panel) { root.appendChild(h("pre", { class: "card small", text: JSON.stringify(payload.data, null, 2) })); return; }
        return loadPanel(view.id).then(function () {
          if (my !== seq) return;
          try { MS.getPanel(view.id)(root, payload.data, { params: route.params, applied: payload.params, header: payload.header, view: view, go: function (p) { go(view.id, Object.assign({}, route.params, p)); } }); }
          catch (e) { showState(root, "error", "面板渲染失败", String(e && e.message || e)); }
        }).catch(function (e) { showState(root, "error", "面板无法加载", String(e && e.message || e)); });
      }
      var err = payload.error || {};
      showState(root, payload.status === "unavailable" ? "unavailable" : "error", payload.status === "unavailable" ? "数据不可用" : "视图出错", err.message || "未知错误", err.code);
    }).catch(function (e) {
      if (my !== seq) return;
      root.textContent = "";
      showState(root, "error", "无法连接服务", String(e && e.message || e));
    });
  }


  /* ---- 股票详情弹窗：点任何表格里的代码打开；只读视图 stock 取数并用其面板渲染。
     不改 hash（页面与可返回性不受影响）；Esc / 点背景 / 关闭按钮可关；窄屏全屏；关闭后焦点回到触发处。 ---- */
  var stockModal = null, stockSeq = 0;
  function closeStock() {
    if (!stockModal) return;
    var m = stockModal;
    stockModal = null; stockSeq += 1;
    document.removeEventListener("keydown", m.onKey, true);
    if (m.root.parentNode) m.root.parentNode.removeChild(m.root);
    document.body.classList.remove("modal-open");
    try { if (m.opener && m.opener.focus && document.body.contains(m.opener)) m.opener.focus(); } catch (e) { /* 焦点恢复失败不影响使用 */ }
  }
  /* 通用弹窗外壳：复盘卡、股票详情共用同一个弹窗槽（同一时间只开一个）；返回 {body,title,my}。 */
  function openShell(titleText, opener) {
    closeStock();
    var my = ++stockSeq;
    var body = h("div", { class: "modal-body" }, MS.loading());
    var title = h("h2", { id: "stock-modal-title", text: titleText });
    var closeBtn = h("button", { type: "button", class: "btn modal-close", "aria-label": "关闭详情", text: "关闭 ✕" });
    var dialog = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": "stock-modal-title" }, [h("div", { class: "modal-head" }, [title, closeBtn]), body]);
    var backdrop = h("div", { class: "modal-backdrop" }, dialog);
    var downOnBackdrop = false;
    backdrop.addEventListener("mousedown", function (e) { downOnBackdrop = e.target === backdrop; });
    backdrop.addEventListener("click", function (e) { if (downOnBackdrop && e.target === backdrop) closeStock(); downOnBackdrop = false; });
    closeBtn.addEventListener("click", closeStock);
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeStock(); return; }
      if (e.key !== "Tab") return;
      var f = Array.prototype.filter.call(dialog.querySelectorAll("a[href],button,select,input,[tabindex]"), function (x) { return x.getAttribute("tabindex") !== "-1" && !x.disabled; });
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (!dialog.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
      else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(backdrop);
    document.body.classList.add("modal-open");
    stockModal = { root: backdrop, opener: opener || null, onKey: onKey };
    closeBtn.focus();
    return { body: body, title: title, my: my };
  }
  function openStock(code, opener) {
    var sh = openShell(MS.codeLabel(code), opener), body = sh.body, title = sh.title, my = sh.my;
    getJSON("/api/v/stock?code=" + encodeURIComponent(code)).then(function (payload) {
      if (my !== stockSeq) return;
      body.textContent = "";
      if (payload.status !== "ok") {
        var err = payload.error || {};
        showState(body, payload.status === "unavailable" ? "unavailable" : "error", payload.status === "unavailable" ? "数据不可用" : "详情无法显示", err.message || "未知错误", err.code);
        return;
      }
      if (payload.data && payload.data.code_names) MS.setNames(payload.data.code_names);
      title.textContent = MS.codeLabel(code);
      if (payload.header) body.appendChild(freshCard(payload.header));
      return loadPanel("stock").then(function () {
        if (my !== stockSeq) return;
        try { MS.getPanel("stock")(body, payload.data, { params: { code: code }, applied: payload.params, header: payload.header, view: { id: "stock" }, modal: true, go: function () {} }); }
        catch (e) { showState(body, "error", "面板渲染失败", String(e && e.message || e)); }
      });
    }).catch(function (e) {
      if (my !== stockSeq) return;
      body.textContent = "";
      showState(body, "error", "详情无法加载", String(e && e.message || e));
    });
  }
  MS.openStock = openStock;
  MS.getJSON = getJSON;
  MS.loadPanel = loadPanel;
  /* 唯一的 POST：拉起复盘卡 AI 评价（服务端只校验后启动 CLI）；带自定义头，浏览器跨站表单发不出这个头。 */
  MS.postJSON = function (url, body) {
    return fetch(url, { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json", "X-MyStock2-Action": "review" }, body: JSON.stringify(body || {}), cache: "no-store" })
      .then(function (r) {
        return r.json().catch(function () { return { status: "error", error: { code: "bad_response", message: "服务器返回了无法解析的内容（HTTP " + r.status + "）" } }; })
          .then(function (j) { j._http = r.status; return j; });
      });
  };
  /* 在弹窗里显示一块已构造好的内容（复盘卡等）：MS.openDialog(标题, 节点, 触发元素)。 */
  MS.openDialog = function (titleText, node, opener) { var sh = openShell(titleText, opener || document.activeElement); sh.body.textContent = ""; sh.body.appendChild(node); };

  function init() {
    var btn = $("theme-btn");
    if (btn) btn.addEventListener("click", function () { MSTheme.cycle(); });
    MSTheme.refresh();
    getJSON("/api/views").then(function (j) {
      state.views = j.views || [];
      state.byId = {}; state.views.forEach(function (v) { state.byId[v.id] = v; });
      var banner = $("banner"), msgs = [];
      if (j.db && j.db.state === "missing") msgs.push(j.db.message);
      (j.problems || []).forEach(function (p) { msgs.push("视图问题（" + p.view + "）：" + p.message); });
      if (msgs.length) { banner.hidden = false; banner.textContent = msgs.join("　|　"); }
      window.addEventListener("hashchange", function () { closeStock(); render(); });
      render();
    }).catch(function (e) {
      var banner = $("banner"); banner.hidden = false; banner.textContent = "无法连接服务：" + e;
    });
  }
  init();
})();
