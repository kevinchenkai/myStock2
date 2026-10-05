/* 公共前端工具（原生 JS，无外部依赖）：DOM 构造、单元渲染、表格、键值、折线图（纯 SVG）、十进制字符串格式化。
   约定：服务器给的金额都是规范十进制字符串；展示文本由服务器生成（带币种）；这里只在图表里对字符串做舍入格式化。
   所有文本一律用 textContent / createTextNode，不拼 HTML 字符串。 */
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";
  var panels = {};

  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k.slice(0, 2) === "on" && typeof v === "function") el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? "" : v);
      });
    }
    append(el, children);
    return el;
  }
  function append(el, children) {
    if (children === null || children === undefined || children === false) return;
    if (Array.isArray(children)) { children.forEach(function (c) { append(el, c); }); return; }
    el.appendChild(children.nodeType ? children : document.createTextNode(String(children)));
  }
  function svg(tag, attrs, children) {
    var el = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    append(el, children);
    return el;
  }

  /* ---- 单元：{text, v, ccy, dir, tag, title, na, fx} 或原始值 ---- */
  function cell(c) {
    if (c === null || c === undefined) return h("span", { class: "na", text: "—" });
    if (typeof c !== "object") return document.createTextNode(String(c));
    var cls = [];
    if (c.na) cls.push("na");
    else if (c.fx) cls.push("fx", "amt");
    else if (c.ccy || c.v !== undefined) cls.push("amt");
    if (c.dir) cls.push("dir-" + c.dir);
    var span = h("span", { class: cls.join(" "), title: c.title || null, text: c.text });
    if (c.tag) return h("span", null, [span, h("span", { class: "tag", text: c.tag })]);
    return span;
  }

  function badge(text, kind) { return h("span", { class: "badge " + (kind || ""), text: text }); }
  function card(title, body, opts) {
    return h("div", { class: "card" + (opts && opts.cls ? " " + opts.cls : "") }, [title ? h("h2", { text: title }) : null, body]);
  }
  function note(text) { return h("p", { class: "muted small", text: text }); }
  function notes(list) {
    if (!list || !list.length) return null;
    return h("ul", { class: "plain muted small" }, list.map(function (t) { return h("li", { text: t }); }));
  }
  function kv(pairs) {
    return h("div", { class: "kv" }, pairs.map(function (p) {
      return h("div", null, [h("div", { class: "k", text: p[0] }), h("div", { class: "v" }, cell(p[1]))]);
    }));
  }

  /* 表格：columns=[{key,label,num}]；窄屏由 CSS 折成卡片（用 data-label） */
  function table(columns, rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) return h("p", { class: "muted", text: opts.empty || "（暂无数据）" });
    var thead = h("thead", null, h("tr", null, columns.map(function (c) { return h("th", { class: c.num ? "num" : null, text: c.label }); })));
    var tbody = h("tbody", null, rows.map(function (r) {
      return h("tr", null, columns.map(function (c) {
        var v = typeof c.render === "function" ? c.render(r) : r[c.key];
        return h("td", { class: c.num ? "num" : null, "data-label": c.label }, cell(v));
      }));
    }));
    return h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" + (columns.length >= 8 ? " wide" : "") }, [thead, tbody]));
  }

  /* ---- 十进制字符串格式化（不经 float；银行家舍入） ---- */
  function fmtDec(s, dp) {
    if (s === null || s === undefined) return "—";
    var str = String(s), neg = false;
    if (str[0] === "-") { neg = true; str = str.slice(1); }
    var parts = str.split("."), ip = parts[0] || "0", fp = parts[1] || "";
    var keep, rest;
    if (fp.length <= dp) { keep = ip + (fp + new Array(dp + 1).join("0")).slice(0, dp); rest = ""; }
    else { keep = ip + fp.slice(0, dp); rest = fp.slice(dp); }
    var n = BigInt(keep || "0");
    if (rest) {
      var first = rest.charAt(0), tail = /[1-9]/.test(rest.slice(1));
      if (first > "5" || (first === "5" && tail) || (first === "5" && !tail && (n % 2n === 1n))) n += 1n;
    }
    var digits = n.toString();
    if (dp > 0) { while (digits.length <= dp) digits = "0" + digits; }
    var i = dp > 0 ? digits.slice(0, digits.length - dp) : digits, f = dp > 0 ? digits.slice(digits.length - dp) : "";
    i = i.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    var out = i + (f ? "." + f : "");
    if (neg && /[1-9]/.test(out)) out = "-" + out;
    return out;
  }
  function fmtMoney(s, ccy) { return s === null || s === undefined ? "不可用" : fmtDec(s, 2) + " " + ccy; }
  function fmtTime(iso) {
    if (!iso) return "未知";
    var m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
    return m ? m[1] + " " + m[2] + " UTC" : iso;
  }

  /* ---- 折线图（纯 SVG）。缺口（y 为 null）断开折线；gap 日用灰带标出；flows 用小三角标记。
     opts: {series:[{name,color,dash,points:[{x,y,note}]}], xs:[date...], gaps:{date:text}, marks:[{x,label}], ccy, height}
     点的 y 是十进制字符串或 null；仅绘图时转 Number，所有展示文本仍由字符串格式化。 ---- */
  function lineChart(host, opts) {
    var xs = opts.xs, series = opts.series, ccy = opts.ccy || "";
    var wrap = h("div", { class: "chart" });
    var readout = h("div", { class: "readout", "aria-live": "polite", text: "移动鼠标或点按图表查看每日数值" });
    host.appendChild(wrap);
    host.appendChild(readout);
    function draw() {
      wrap.textContent = "";
      var W = Math.max(280, wrap.clientWidth || host.clientWidth || 320), H = opts.height || 240;
      var L = 52, R = 8, T = 8, B = 22, pw = W - L - R, ph = H - T - B;
      var vals = [];
      series.forEach(function (s) { s.points.forEach(function (p) { if (p.y !== null && p.y !== undefined) vals.push(Number(p.y)); }); });
      var lo = vals.length ? Math.min.apply(null, vals) : 0, hi = vals.length ? Math.max.apply(null, vals) : 1;
      if (lo === hi) { lo -= 1; hi += 1; }
      var pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
      var n = xs.length, step = n > 1 ? pw / (n - 1) : pw;
      function X(i) { return L + (n > 1 ? i * step : pw / 2); }
      function Y(v) { return T + ph - (v - lo) / (hi - lo) * ph; }
      var root = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": opts.label || "趋势图", preserveAspectRatio: "xMidYMid meet" });
      for (var t = 0; t <= 4; t++) {
        var v = lo + (hi - lo) * t / 4, y = Y(v);
        root.appendChild(svg("line", { x1: L, x2: W - R, y1: y, y2: y, class: "axis", "stroke-width": 0.5 }));
        root.appendChild(svg("text", { x: L - 4, y: y + 3, "text-anchor": "end", class: "tick" }, compact(v)));
      }
      [0, Math.floor((n - 1) / 2), n - 1].forEach(function (i, k) {
        if (i < 0 || (k > 0 && i === 0)) return;
        root.appendChild(svg("text", { x: X(i), y: H - 6, "text-anchor": k === 0 ? "start" : k === 2 ? "end" : "middle", class: "tick" }, xs[i]));
      });
      xs.forEach(function (d, i) {                               // 缺口：灰带，不连线
        if (opts.gaps && opts.gaps[d]) root.appendChild(svg("rect", { x: X(i) - Math.max(step / 2, 1.5), y: T, width: Math.max(step, 3), height: ph, class: "gapband" }, svg("title", {}, d + " 缺口：" + opts.gaps[d])));
      });
      series.forEach(function (s) {
        var d = "", pen = false, run = 0, lastI = -1;
        s.points.forEach(function (p, i) {
          if (p.y === null || p.y === undefined) { if (run === 1 && lastI >= 0) root.appendChild(svg("circle", { cx: X(lastI), cy: Y(Number(s.points[lastI].y)), r: 2.5, fill: s.color })); pen = false; run = 0; return; }
          d += (pen ? "L" : "M") + X(i).toFixed(1) + " " + Y(Number(p.y)).toFixed(1) + " ";
          pen = true; run += 1; lastI = i;
        });
        if (run === 1 && lastI >= 0) root.appendChild(svg("circle", { cx: X(lastI), cy: Y(Number(s.points[lastI].y)), r: 2.5, fill: s.color }));
        root.appendChild(svg("path", { d: d, fill: "none", stroke: s.color, "stroke-width": 1.8, "stroke-dasharray": s.dash || "none", "stroke-linejoin": "round" }));
      });
      (opts.marks || []).forEach(function (m) {
        var i = xs.indexOf(m.x); if (i < 0) return;
        root.appendChild(svg("path", { d: "M" + (X(i) - 4) + " " + (T + ph) + " L" + (X(i) + 4) + " " + (T + ph) + " L" + X(i) + " " + (T + ph - 8) + " Z", class: "flow" }, svg("title", {}, m.label)));
      });
      var cursor = svg("line", { x1: 0, x2: 0, y1: T, y2: T + ph, class: "cursor", visibility: "hidden" });
      root.appendChild(cursor);
      var hit = svg("rect", { x: L, y: T, width: pw, height: ph, fill: "transparent" });
      hit.style.touchAction = "pan-y";
      function at(ev) {
        var pt = ev.touches ? ev.touches[0] : ev, r = root.getBoundingClientRect();
        var x = (pt.clientX - r.left) * (W / r.width);
        var i = n > 1 ? Math.round((x - L) / step) : 0; i = Math.max(0, Math.min(n - 1, i));
        cursor.setAttribute("x1", X(i)); cursor.setAttribute("x2", X(i)); cursor.setAttribute("visibility", "visible");
        readout.textContent = "";
        readout.appendChild(document.createTextNode(xs[i] + (opts.gaps && opts.gaps[xs[i]] ? "　缺口：" + opts.gaps[xs[i]] : "")));
        series.forEach(function (s) {
          var p = s.points[i];
          var shown = p && p.y !== null && p.y !== undefined ? (opts.fmt ? opts.fmt(p.y) : fmtMoney(p.y, ccy)) : "不可用";
          readout.appendChild(h("div", null, s.name + "：" + shown));
        });
      }
      ["mousemove", "mousedown", "touchstart", "touchmove"].forEach(function (e) { hit.addEventListener(e, at, { passive: true }); });
      root.appendChild(hit);
      wrap.appendChild(root);
    }
    draw();
    var timer = null;
    var onResize = function () { clearTimeout(timer); timer = setTimeout(function () { if (document.body.contains(wrap)) draw(); }, 120); };
    window.addEventListener("resize", onResize);
    return { redraw: draw };
  }
  function compact(v) {
    var a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(1) + "k";
    return v.toFixed(a < 10 ? 2 : 0);
  }
  function legend(items) {
    return h("div", { class: "legend" }, items.map(function (it) {
      var sw = h("span", { class: "sw" });          // 样式经 CSSOM 设置（CSP 不允许内联 style 属性）
      sw.style.borderTopColor = it.color;
      sw.style.borderTopStyle = it.dash ? "dashed" : "solid";
      return h("span", null, [sw, it.name]);
    }));
  }

  window.MS = {
    h: h, svg: svg, cell: cell, badge: badge, card: card, note: note, notes: notes, kv: kv, table: table,
    fmtDec: fmtDec, fmtMoney: fmtMoney, fmtTime: fmtTime, lineChart: lineChart, legend: legend,
    registerPanel: function (id, fn) { panels[id] = fn; },
    getPanel: function (id) { return panels[id]; }
  };
})();
