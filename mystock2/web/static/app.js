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

  function renderNav(current) {
    var nav = $("nav");
    nav.textContent = "";
    state.views.filter(function (v) { return !v.hidden; }).forEach(function (v) {
      nav.appendChild(h("a", { href: "#/" + encodeURIComponent(v.id), text: v.title, "aria-current": v.id === current ? "page" : null, title: v.description || null }));
    });
  }

  function renderParams(view, payload, params) {
    var box = $("params");
    box.textContent = "";
    var data = payload && payload.data;
    var applied = (payload && payload.params) || {};
    function field(name, label, control) { return h("label", null, [label, control]); }
    (view.params || []).forEach(function (p) {
      var cur = params[p.name] !== undefined ? params[p.name] : (applied[p.name] !== undefined ? applied[p.name] : p.default);
      var ctl;
      if (p.name === "account") return;                                    // 账户选择器见下
      if (p.name === "symbol" && data && data.codes) {   // 标的选择器：选项来自视图数据（不是配置里的固定列表）
        var ssel = h("select", { "aria-label": "symbol" }, [h("option", { value: "", text: "（自动）" })].concat(data.codes.map(function (c) {
          return h("option", { value: c, selected: c === data.symbol ? true : null, text: c });
        })));
        ssel.addEventListener("change", function () { var np = Object.assign({}, params); np.symbol = ssel.value; go(view.id, np); });
        box.appendChild(field("symbol", "标的", ssel));
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
      box.appendChild(field(p.name, p.description ? p.name + "（" + p.description.split("（")[0] + "）" : p.name, ctl));
    });
    if (data && data.accounts && data.accounts.length > 1) {
      var sel = h("select", { "aria-label": "account" }, data.accounts.map(function (a) { return h("option", { value: a, selected: a === data.account_id ? true : null, text: a }); }));
      sel.addEventListener("change", function () { var np = Object.assign({}, params); np.account = sel.value; go(view.id, np); });
      box.appendChild(field("account", "账户", sel));
    }
  }

  function renderFresh(header) {
    var box = $("fresh");
    box.textContent = "";
    if (!header) return;
    var s = header.staleness || {}, kind = s.label === "新鲜" ? "fresh-ok" : s.label === "陈旧" ? "fresh-stale" : "fresh-unknown";
    var grid = h("div", { class: "fresh" }, [
      h("div", null, [h("div", { class: "k", text: "数据模式" }), h("div", { class: "v", text: header.data_mode_label || "未知" })]),
      h("div", null, [h("div", { class: "k", text: "事件时间" }), h("div", { class: "v", text: MS.fmtTime(header.event_at) })]),
      h("div", null, [h("div", { class: "k", text: "采集时间" }), h("div", { class: "v", text: MS.fmtTime(header.collected_at) })]),
      h("div", null, [h("div", { class: "k", text: "陈旧度" }), h("div", { class: "v" }, MS.badge(s.label || "未知", kind))]),
    ]);
    var kids = [grid, h("p", { class: "muted small", text: s.text || "" })];
    if (header.sources && header.sources.length) {
      kids.push(h("details", { class: "src" }, [h("summary", { text: "各来源时间（" + header.sources.length + "）" }),
        MS.table([{ key: "name", label: "来源" }, { key: "event_at", label: "事件时间", render: function (r) { return MS.fmtTime(r.event_at); } },
          { key: "collected_at", label: "采集时间", render: function (r) { return MS.fmtTime(r.collected_at); } }, { key: "text", label: "陈旧度" }], header.sources)]));
    }
    if (header.notes && header.notes.length) kids.push(MS.notes(header.notes));
    box.appendChild(h("div", { class: "card" }, kids));
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

  function showState(root, kind, title, text, code) {
    root.appendChild(h("div", { class: "state " + (kind === "error" ? "error" : "") }, [h("strong", { text: title }), h("div", { text: text }), code ? h("div", { class: "small", text: "状态码：" + code }) : null]));
  }

  var seq = 0;
  function render() {
    var route = parseHash(), my = ++seq;
    if (!route.id && state.views.length) { var first = state.views.filter(function (v) { return !v.hidden; })[0] || state.views[0]; return go(first.id, {}); }
    var view = state.byId[route.id], root = $("panel");
    renderNav(route.id);
    root.textContent = ""; $("fresh").textContent = ""; $("params").textContent = "";
    if (!view) { showState(root, "unavailable", "没有这个视图", route.id || "（空）", "unknown_view"); return; }
    document.title = view.title + " · myStock2";
    renderParams(view, null, route.params);
    root.appendChild(h("p", { class: "muted", text: "加载中…" }));
    var qs = new URLSearchParams(route.params).toString();
    getJSON("/api/v/" + encodeURIComponent(view.id) + (qs ? "?" + qs : "")).then(function (payload) {
      if (my !== seq) return;
      root.textContent = "";
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
      window.addEventListener("hashchange", render);
      render();
    }).catch(function (e) {
      var banner = $("banner"); banner.hidden = false; banner.textContent = "无法连接服务：" + e;
    });
  }
  init();
})();
