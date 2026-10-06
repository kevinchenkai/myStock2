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
    if (c.subs && c.subs.length) {                                  // 副行：若干带涨跌色的小字（如相对基准的位置）
      return h("div", null, [span, h("div", { class: "small sub" }, c.subs.map(function (x, i) {
        return h("span", null, [i ? " ／ " : "", h("span", { class: "amt" + (x.dir ? " dir-" + x.dir : ""), text: x.text })]);
      }))]);
    }
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

  /* ---- 标的名称与详情入口：row.code_name 由服务器按代码补上（instrument_name）；这里只负责显示，排序/市场筛选仍只用 code。 ---- */
  var names = {};
  function setNames(m) { if (m && typeof m === "object") Object.keys(m).forEach(function (k) { names[k] = m[k]; }); }
  function isCode(s) { return typeof s === "string" && /^(US|HK)\.[A-Z0-9][A-Z0-9.\-]*$/.test(s); }
  function nameOf(code) { return isCode(code) && names[code] ? names[code] : ""; }
  function codeLabel(code) { var n = nameOf(code); return n ? code + " " + n : String(code); }
  function codeNode(r, inner) {
    var code = r.code, nm = r.code_name || nameOf(code);
    var link = h("span", { class: "code-link", role: "button", tabindex: "0", title: "查看 " + code + " 的详情", "aria-label": "查看 " + code + (nm ? " " + nm : "") + " 的详情" }, inner);
    function open(e) { if (e && e.stopPropagation) e.stopPropagation(); if (typeof window.MS.openStock === "function") window.MS.openStock(code, link); }
    link.addEventListener("click", open);
    link.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(e); } });
    return nm ? h("div", { class: "code-cell" }, [link, h("div", { class: "small muted code-name", text: nm })]) : link;
  }

  /* 表格：columns=[{key,label,num}]；窄屏由 CSS 折成卡片（用 data-label）。
       · 「code」列：显示中文名小字副行，点击代码弹出该标的详情（MS.openStock，由 app.js 提供）；
     交互（纯前端，只改显示顺序/可见行，不改数据）：
       · 点表头排序（再点切换升/降；第三次点恢复服务器原顺序）；窄屏表头被折叠，用工具条的「排序」下拉；
       · 市场筛选（opts.market === false 可关闭，如单一标的的弹窗）：行里有 code（US./HK. 前缀）或 currency（USD/HKD）时，工具条出现「全部 / 美股 / 港股」；选择记在 localStorage（可选），各表共用；
         没有市场归属的行（如汇总行）始终显示。
       · 列筛选（opts.filters=[{key,label}]）：按该列显示文本的取值生成下拉（全部 + 各取值及行数），多个筛选取交集；选项不记忆，换页重置。
     排序键：单元的 v（规范十进制字符串，转 Number 只用于比较）；没有 v 则取文本；不可用/缺失一律排最后。 */
  var MARKET_KEY = "mystock2.market";
  function marketOf(r) {
    if (!r || typeof r !== "object") return null;
    var c = r.code;
    if (c && typeof c === "object") c = c.text;
    var m = typeof c === "string" ? /^(US|HK)\./.exec(c) : null;
    if (m) return m[1];
    var ccy = r.currency;
    if (ccy && typeof ccy === "object") ccy = ccy.text;
    return ccy === "USD" ? "US" : ccy === "HKD" ? "HK" : null;
  }
  function storedMarket() { try { return window.localStorage.getItem(MARKET_KEY) || "ALL"; } catch (e) { return "ALL"; } }
  function storeMarket(m) { try { window.localStorage.setItem(MARKET_KEY, m); } catch (e) { /* 无存储也能用 */ } }
  function sortValue(c, r) {
    var v = typeof c.render === "function" ? c.render(r) : r[c.key];
    if (v === null || v === undefined) return null;
    if (typeof v === "object") {
      if (v.na) return null;
      if (v.v !== undefined && v.v !== null && /^-?\d/.test(String(v.v))) return Number(v.v);
      v = v.text;
      if (v === undefined || v === null) return null;
    }
    var str = String(v);
    if (/^[+-]?[\d,]+(\.\d+)?\s*[A-Za-z%]*$/.test(str)) return Number(str.replace(/[,+A-Za-z%\s]/g, ""));
    return str;
  }
  function cellText(c, r) {
    var v = typeof c.render === "function" ? c.render(r) : r[c.key];
    if (v === null || v === undefined) return "";
    if (typeof v === "object") return v.na ? "" : (v.text === undefined || v.text === null ? "" : String(v.text));
    return String(v);
  }
  function compareValues(a, b) {
    if (a === null && b === null) return 0;
    if (a === null) return 1;                       // 缺失永远在最后（不论升降）
    if (b === null) return -1;
    if (typeof a === "number" && typeof b === "number") return a - b;
    return String(a).localeCompare(String(b), "zh");
  }
  /* 翻页：行数超过每页条数时在表下方出现翻页条（首页/上一页/页码/下一页/末页 + 每页条数）；与排序、市场筛选配合（先筛选、再排序、最后切页）。
     每页条数记在 localStorage（可选）。opts.pageSize 指定默认值，opts.paginate === false 关闭；opts.onRowClick(row) 给每一行绑定点击（排序/翻页重绘后仍有效）。 */
  var PAGE_KEY = "mystock2.pageSize", PAGE_SIZES = [20, 50, 100, 200];
  function storedPageSize(def) {
    try { var v = parseInt(window.localStorage.getItem(PAGE_KEY), 10); if (PAGE_SIZES.indexOf(v) >= 0) return v; } catch (e) { /* 无存储也能用 */ }
    return def;
  }
  function storePageSize(n) { try { window.localStorage.setItem(PAGE_KEY, String(n)); } catch (e) { /* ignore */ } }
  function table(columns, rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) return h("p", { class: "muted", text: opts.empty || "（暂无数据）" });
    var host = h("div", { class: "tbl-host" });
    var state = { sortKey: null, dir: 1, market: storedMarket(), page: 1, filters: {}, pageSize: opts.paginate === false ? 0 : storedPageSize(opts.pageSize || 50) };
    var pagerEl = h("div", { class: "pager" });
    var hasMarket = opts.market !== false && rows.length > 1 && rows.filter(function (r) { return marketOf(r); }).length * 2 >= rows.length;
    var canSort = opts.sortable !== false && rows.length > 1;
    var filterCols = (opts.filters || []).map(function (f) { return columns.filter(function (c) { return c.key === f.key; })[0]; }).filter(Boolean);
    var tableEl = h("table", { class: "tbl" + (columns.length >= 8 ? " wide" : "") });
    var wrap = h("div", { class: "tbl-wrap" }, tableEl);
    var toolbar = null, selectEl = null, dirBtn = null, countEl = null;
    function visibleRows() {
      var out = rows.filter(function (r) { var m = marketOf(r); return !hasMarket || state.market === "ALL" || m === null || m === state.market; });
      filterCols.forEach(function (c) {
        var want = state.filters[c.key];
        if (want !== undefined && want !== "") out = out.filter(function (r) { return cellText(c, r) === want; });
      });
      if (state.sortKey !== null) {
        var col = columns.filter(function (c) { return c.key === state.sortKey; })[0];
        if (col) {
          var keyed = out.map(function (r, i) { return { r: r, i: i, k: sortValue(col, r) }; });
          keyed.sort(function (x, y) {
            if (x.k === null || y.k === null) return compareValues(x.k, y.k) || x.i - y.i;   // 缺失在后，与方向无关
            return compareValues(x.k, y.k) * state.dir || x.i - y.i;
          });
          out = keyed.map(function (x) { return x.r; });
        }
      }
      return out;
    }
    function draw() {
      var all = visibleRows(), shown = all;
      var pages = state.pageSize ? Math.max(1, Math.ceil(all.length / state.pageSize)) : 1;
      if (state.page > pages) state.page = pages;
      if (state.pageSize && all.length > state.pageSize) shown = all.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);
      drawPager(all.length, pages);
      var thead = h("thead", null, h("tr", null, columns.map(function (c) {
        var active = state.sortKey === c.key;
        var th = h("th", { class: (c.num ? "num" : "") + (canSort ? " sortable" : "") + (active ? " sorted" : ""),
                           "aria-sort": active ? (state.dir === 1 ? "ascending" : "descending") : null,
                           title: canSort ? "点击排序" : null }, c.label + (active ? (state.dir === 1 ? " ▲" : " ▼") : ""));
        if (canSort) th.addEventListener("click", function () { sortBy(c.key); });
        return th;
      })));
      var tbody = h("tbody", null, shown.map(function (r) {
        var tr = h("tr", null, columns.map(function (c) {
          var v = typeof c.render === "function" ? c.render(r) : r[c.key];
          var node = cell(v);
          if (c.key === "code" && isCode(r.code)) node = codeNode(r, node);
          return h("td", { class: c.num ? "num" : null, "data-label": c.label }, node);
        }));
        if (typeof opts.onRowClick === "function") {
          tr.setAttribute("tabindex", "0");
          tr.setAttribute("role", "button");
          tr.addEventListener("click", function (e) { if (!(e.target && e.target.closest && e.target.closest("a,button"))) opts.onRowClick(r); });
          tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); opts.onRowClick(r); } });
        }
        return tr;
      }));
      tableEl.textContent = "";
      tableEl.appendChild(thead);
      tableEl.appendChild(tbody);
      if (countEl) countEl.textContent = all.length === rows.length ? rows.length + " 行" : all.length + " / " + rows.length + " 行";
      if (selectEl) selectEl.value = state.sortKey === null ? "" : state.sortKey;
      if (dirBtn) dirBtn.textContent = state.dir === 1 ? "升序 ▲" : "降序 ▼";
    }
    function drawPager(total, pages) {
      pagerEl.textContent = "";
      if (!state.pageSize || total <= state.pageSize) return;          // 行数不超过每页条数：不显示翻页条
      function go(n) { return function () { state.page = Math.min(pages, Math.max(1, n)); draw(); }; }
      function btn(label, n, disabled) {
        var b = h("button", { type: "button", class: "seg-btn", text: label, disabled: disabled ? true : null });
        if (!disabled) b.addEventListener("click", go(n));
        return b;
      }
      var from = (state.page - 1) * state.pageSize + 1, to = Math.min(total, state.page * state.pageSize);
      var sizeSel = h("select", { "aria-label": "每页条数" }, PAGE_SIZES.map(function (n) { return h("option", { value: String(n), selected: n === state.pageSize ? true : null, text: n + " 条/页" }); }));
      sizeSel.addEventListener("change", function () { state.pageSize = parseInt(sizeSel.value, 10); storePageSize(state.pageSize); state.page = 1; draw(); });
      var jump = h("select", { "aria-label": "页码" }, Array.apply(null, { length: pages }).map(function (_, i) { return h("option", { value: String(i + 1), selected: i + 1 === state.page ? true : null, text: "第 " + (i + 1) + " 页" }); }));
      jump.addEventListener("change", function () { go(parseInt(jump.value, 10))(); });
      pagerEl.appendChild(h("span", { class: "muted small", text: "第 " + from + "–" + to + " 行，共 " + total + " 行" }));
      pagerEl.appendChild(h("span", { class: "pager-ctl" }, [btn("«", 1, state.page === 1), btn("‹ 上一页", state.page - 1, state.page === 1), jump, h("span", { class: "muted small", text: "/ " + pages + " 页" }),
        btn("下一页 ›", state.page + 1, state.page === pages), btn("»", pages, state.page === pages), sizeSel]));
    }
    function sortBy(key) {
      var first = columns.filter(function (c) { return c.key === key; })[0].num ? -1 : 1;     // 数值列先降序（大的在前），文本列先升序
      if (state.sortKey === key) {
        if (state.dir === first) state.dir = -first;             // 第二次点：反向
        else { state.sortKey = null; state.dir = 1; }            // 第三次点：恢复原顺序
      } else { state.sortKey = key; state.dir = first; }
      state.page = 1;
      draw();
    }
    if (hasMarket || canSort || filterCols.length) {
      var kids = [];
      filterCols.forEach(function (c) {
        var counts = {}, order = [];
        rows.forEach(function (r) { var t = cellText(c, r); if (!(t in counts)) { counts[t] = 0; order.push(t); } counts[t] += 1; });
        var sel = h("select", { "aria-label": c.label + "筛选" }, [h("option", { value: "", text: c.label + "：全部" })].concat(order.map(function (t) {
          return h("option", { value: t, text: (t || "—") + "（" + counts[t] + "）" });
        })));
        sel.addEventListener("change", function () { state.filters[c.key] = sel.value; state.page = 1; draw(); });
        kids.push(h("span", { class: "sortbox" }, sel));
      });
      if (hasMarket) {
        kids.push(h("span", { class: "seg", role: "group", "aria-label": "市场筛选" }, [["ALL", "全部"], ["US", "美股"], ["HK", "港股"]].map(function (m) {
          var b = h("button", { type: "button", class: "seg-btn" + (state.market === m[0] ? " on" : ""), text: m[1], "aria-pressed": state.market === m[0] ? "true" : "false" });
          b.addEventListener("click", function () {
            state.market = m[0]; storeMarket(m[0]); state.page = 1;
            Array.prototype.forEach.call(b.parentNode.children, function (x) { var on = x === b; x.className = "seg-btn" + (on ? " on" : ""); x.setAttribute("aria-pressed", on ? "true" : "false"); });
            draw();
          });
          return b;
        })));
      }
      if (canSort) {
        selectEl = h("select", { "aria-label": "排序列" }, [h("option", { value: "", text: "原顺序" })].concat(columns.map(function (c) { return h("option", { value: c.key, text: c.label }); })));
        selectEl.addEventListener("change", function () {
          state.sortKey = selectEl.value === "" ? null : selectEl.value;
          state.dir = state.sortKey && columns.filter(function (c) { return c.key === state.sortKey; })[0].num ? -1 : 1;
          state.page = 1;
          draw();
        });
        dirBtn = h("button", { type: "button", class: "seg-btn", text: "升序 ▲", title: "切换升序/降序" });
        dirBtn.addEventListener("click", function () { if (state.sortKey !== null) { state.dir = -state.dir; draw(); } });
        kids.push(h("span", { class: "sortbox" }, [h("span", { class: "muted small", text: "排序" }), selectEl, dirBtn]));
      }
      countEl = h("span", { class: "muted small count" });
      kids.push(countEl);
      toolbar = h("div", { class: "tbl-toolbar" }, kids);
      host.appendChild(toolbar);
    }
    host.appendChild(wrap);
    host.appendChild(pagerEl);
    draw();
    return host;
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
  function fmtPx(s) {                                   // 价格：至少 2 位、至多 4 位小数（字符串处理，不经 float）
    var t = fmtDec(s, 4), m = /^(-?[\d,]+)\.(\d+)$/.exec(t);
    if (!m) return t;
    var f = m[2].replace(/0+$/, "");
    while (f.length < 2) f += "0";
    return m[1] + "." + f;
  }
  function fmtMoney(s, ccy) { return s === null || s === undefined ? "不可用" : fmtDec(s, 2) + " " + ccy; }
  function fmtTime(iso) {
    if (!iso) return "未知";
    var m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
    return m ? m[1] + " " + m[2] + " UTC" : iso;
  }

  /* ---- 折线图（纯 SVG）。缺口（y 为 null）断开折线；gap 日用灰带标出；flows 用小三角标记。
     opts: {series:[{name,color,dash,points:[{x,y,note}]}], xs:[date...], gaps:{date:text}, marks:[{x,label}|{x,side:"BUY"|"SELL",y,label}], ccy, height}
     marks：只有 x/label 的是底部小三角（如外部资金流）；带 side/y 的是买卖标记（买＝向上三角、卖＝向下三角，落在成交均价处，颜色不用红绿）。
     点按/悬停图表时，读数里列出该日所有标记的说明。
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
      (opts.marks || []).forEach(function (m) { if (m.side && m.y !== null && m.y !== undefined && xs.indexOf(m.x) >= 0) vals.push(Number(m.y)); });
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
        if (m.side) {                                              // 买卖标记：落在成交均价处，三角贴在价格点的下方（买）/上方（卖），不遮住折线
          var cy = Y(Number(m.y)), buy = m.side === "BUY", mx = X(i);
          var tri = buy ? "M" + (mx - 5) + " " + (cy + 9) + " L" + (mx + 5) + " " + (cy + 9) + " L" + mx + " " + (cy + 1) + " Z"
                        : "M" + (mx - 5) + " " + (cy - 9) + " L" + (mx + 5) + " " + (cy - 9) + " L" + mx + " " + (cy - 1) + " Z";
          root.appendChild(svg("path", { d: tri, class: buy ? "mark-buy" : "mark-sell" }, svg("title", {}, m.label)));
          return;
        }
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
        (opts.marks || []).forEach(function (m) { if (m.x === xs[i] && m.label) readout.appendChild(h("div", null, m.label)); });
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

  /* ---- 预测带图（纯 SVG）：竖条＝每日实际最低~最高（按涨跌着色），带＝各模型对该日的预测区间，空心圈＝突破点。
     opts: {dates, actual:[{low,high,close,dir}], models:[{name,color,ring,points:[{low,high,breach_low,breach_high}|null]}], ccy, height, label}
     价格是十进制字符串；仅绘图时转 Number，读数文本用字符串格式化。 ---- */
  function bandChart(host, opts) {
    var xs = opts.dates, act = opts.actual, models = opts.models, ccy = opts.ccy || "";
    var wrap = h("div", { class: "chart" });
    var readout = h("div", { class: "readout", "aria-live": "polite", text: "移动鼠标或点按图表查看每日的实际区间与模型预测区间" });
    host.appendChild(wrap);
    host.appendChild(readout);
    function px(s) { return fmtDec(s, 2); }
    function draw() {
      wrap.textContent = "";
      var W = Math.max(280, wrap.clientWidth || host.clientWidth || 320), H = opts.height || 280;
      var L = 52, R = 8, T = 8, B = 22, pw = W - L - R, ph = H - T - B, n = xs.length;
      var vals = [];
      act.forEach(function (a) { vals.push(Number(a.low), Number(a.high)); });
      models.forEach(function (m) { m.points.forEach(function (p) { if (p) { vals.push(Number(p.low), Number(p.high)); } }); });
      var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
      if (lo === hi) { lo -= 1; hi += 1; }
      var pad = (hi - lo) * 0.05; lo -= pad; hi += pad;
      var step = n > 1 ? pw / (n - 1) : pw;
      function X(i) { return L + (n > 1 ? i * step : pw / 2); }
      function Y(v) { return T + ph - (v - lo) / (hi - lo) * ph; }
      var root = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": opts.label || "预测带图", preserveAspectRatio: "xMidYMid meet" });
      for (var t = 0; t <= 4; t++) {
        var v = lo + (hi - lo) * t / 4, y = Y(v);
        root.appendChild(svg("line", { x1: L, x2: W - R, y1: y, y2: y, class: "axis", "stroke-width": 0.5 }));
        root.appendChild(svg("text", { x: L - 4, y: y + 3, "text-anchor": "end", class: "tick" }, compact(v)));
      }
      [0, Math.floor((n - 1) / 2), n - 1].forEach(function (i, k) {
        if (i < 0 || (k > 0 && i === 0)) return;
        root.appendChild(svg("text", { x: X(i), y: H - 6, "text-anchor": k === 0 ? "start" : k === 2 ? "end" : "middle", class: "tick" }, xs[i]));
      });
      models.forEach(function (m) {                                   // 预测带：连续的有预测的日子成一段，缺失处断开，不插值
        var seg = [];
        function flush() {
          if (seg.length) {
            var up = seg.map(function (i) { return X(i).toFixed(1) + " " + Y(Number(m.points[i].high)).toFixed(1); });
            var dn = seg.slice().reverse().map(function (i) { return X(i).toFixed(1) + " " + Y(Number(m.points[i].low)).toFixed(1); });
            var all = up.concat(dn);
            root.appendChild(svg("path", { d: "M" + all.join(" L") + " Z", fill: m.color, "fill-opacity": 0.13, stroke: "none" }));
            root.appendChild(svg("path", { d: "M" + up.join(" L"), fill: "none", stroke: m.color, "stroke-width": 1.1, "stroke-dasharray": m.dash || "none" }));
            root.appendChild(svg("path", { d: "M" + dn.slice().reverse().join(" L"), fill: "none", stroke: m.color, "stroke-width": 1.1, "stroke-dasharray": m.dash || "none" }));
          }
          seg = [];
        }
        m.points.forEach(function (p, i) { if (p) seg.push(i); else flush(); });
        flush();
      });
      var bw = Math.max(1, Math.min(3, step * 0.45));
      act.forEach(function (a, i) {
        root.appendChild(svg("line", { x1: X(i), x2: X(i), y1: Y(Number(a.high)), y2: Y(Number(a.low)), class: "bar-" + (a.dir || "flat"), "stroke-width": bw }));
      });
      models.forEach(function (m) {                                   // 突破点：实际低点跌破预测低点 / 实际高点突破预测高点
        m.points.forEach(function (p, i) {
          if (!p) return;
          if (p.breach_low) root.appendChild(svg("circle", { cx: X(i), cy: Y(Number(act[i].low)), r: m.ring, fill: "none", stroke: m.color, "stroke-width": 1.6 }, svg("title", {}, xs[i] + " " + m.name + "：实际低点跌破预测低点")));
          if (p.breach_high) root.appendChild(svg("circle", { cx: X(i), cy: Y(Number(act[i].high)), r: m.ring, fill: "none", stroke: m.color, "stroke-width": 1.6 }, svg("title", {}, xs[i] + " " + m.name + "：实际高点突破预测高点")));
        });
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
        readout.appendChild(document.createTextNode(xs[i] + "　实际 低 " + px(act[i].low) + " ／ 高 " + px(act[i].high) + " ／ 收 " + px(act[i].close) + " " + ccy));
        models.forEach(function (m) {
          var p = m.points[i], txt;
          if (!p) txt = "不可用（该日没有此模型的预测）";
          else {
            txt = "预测 低 " + px(p.low) + " ／ 高 " + px(p.high) + " " + ccy;
            if (p.breach_low) txt += "　低点跌破";
            if (p.breach_high) txt += "　高点突破";
          }
          readout.appendChild(h("div", null, m.name + "：" + txt));
        });
      }
      ["mousemove", "mousedown", "touchstart", "touchmove"].forEach(function (e) { hit.addEventListener(e, at, { passive: true }); });
      root.appendChild(hit);
      wrap.appendChild(root);
    }
    draw();
    var timer = null;
    window.addEventListener("resize", function () { clearTimeout(timer); timer = setTimeout(function () { if (document.body.contains(wrap)) draw(); }, 120); });
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
    fmtDec: fmtDec, fmtPx: fmtPx, fmtMoney: fmtMoney, fmtTime: fmtTime, setNames: setNames, nameOf: nameOf, codeLabel: codeLabel, lineChart: lineChart, bandChart: bandChart, legend: legend,
    registerPanel: function (id, fn) { panels[id] = fn; },
    getPanel: function (id) { return panels[id]; }
  };
})();
