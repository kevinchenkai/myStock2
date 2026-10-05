"""前端静态约定：无 CDN、深浅色主题、红涨绿跌、窄屏（375px）不截断金额且无横向溢出规则（NF-09）。
真实渲染的 375px 检查在回执里用浏览器截图验证；这里做可自动化的 CSS/HTML/JS 断言。"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mystock2.web import registry

STATIC = Path(registry.__file__).resolve().parent / "static"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS_FILES = sorted(STATIC.glob("*.js")) + sorted(registry.BUILTIN_VIEWS_DIR.glob("*/panel.js"))
BUILTIN = ["account_overview", "holdings", "trades", "pnl", "equity_trend", "fx", "tickets", "scoreboard", "forecast", "replay", "data_status", "stock"]


def block(selector_start: str) -> str:
    i = CSS.index(selector_start)
    return CSS[i: CSS.index("}", i)]


def hex_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def var_in(css_block: str, name: str):
    return hex_rgb(re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", css_block).group(1))


def test_no_external_resources_anywhere():
    for f in [STATIC / "index.html", STATIC / "app.css", *JS_FILES]:
        text = f.read_text(encoding="utf-8").replace("http://www.w3.org/2000/svg", "")      # SVG 命名空间标识符不是网络请求
        assert not re.search(r"https?://|//cdn\.|@import|googleapis|unpkg|jsdelivr", text), f
    assert "<script src=\"/static/" in HTML and "<link rel=\"stylesheet\" href=\"/static/" in HTML


def test_no_inline_scripts_or_styles_or_unsafe_dom_apis():
    assert not re.search(r"<script(?![^>]*\bsrc=)", HTML) and "style=" not in HTML and "onclick" not in HTML
    for f in JS_FILES:
        t = f.read_text(encoding="utf-8")
        assert "innerHTML" not in t and "insertAdjacentHTML" not in t and "eval(" not in t and "document.write" not in t, f
        assert not re.search(r"setAttribute\(\s*[\"']style[\"']", t), f           # CSP 不允许内联 style 属性


def test_viewport_meta_and_language():
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in HTML and 'lang="zh-CN"' in HTML


def test_dark_and_light_themes_follow_system_and_manual_override():
    assert "@media (prefers-color-scheme: dark)" in CSS
    assert ':root:not([data-theme="light"])' in CSS and ':root[data-theme="dark"]' in CSS
    theme = (STATIC / "theme.js").read_text(encoding="utf-8")
    assert "localStorage" in theme and theme.count("try") >= 2 and "catch" in theme       # 记忆选择，读写都容错
    assert all(m in theme for m in ('"auto"', '"light"', '"dark"'))
    assert "theme.js" in HTML and "theme-btn" in HTML
    assert 'body { margin: 0; background: var(--bg)' in CSS                              # body 有显式背景


@pytest.mark.parametrize("scope", [":root {", ":root:not([data-theme=\"light\"]) {", ":root[data-theme=\"dark\"] {"])
def test_red_up_green_down_in_every_theme(scope):
    b = block(scope)
    ur, ug, ub = var_in(b, "--up")
    dr, dg, db_ = var_in(b, "--down")
    assert ur > ug + 40 and ur > ub + 40, "上涨/盈利必须偏红"
    assert dg > dr + 20 and dg > db_ + 10, "下跌/亏损必须偏绿"
    fr, fg, fb = var_in(b, "--fx")
    assert fb >= fr and fb >= fg and abs(fr - fg) < 60, "汇率必须是中性（偏蓝灰）色，不是红/绿"


def test_direction_classes_and_neutral_fx_rules():
    assert re.search(r"\.dir-up\s*\{\s*color:\s*var\(--up\)", CSS) and re.search(r"\.dir-down\s*\{\s*color:\s*var\(--down\)", CSS)
    fx = block(".fx {")
    assert "var(--up)" not in fx and "var(--down)" not in fx and "var(--fx)" in fx
    # 图表曲线颜色不使用红/绿变量（它们表示涨跌，曲线只是类别）
    for v in ("--s1", "--s2", "--s3"):
        r, g, b = var_in(block(":root {"), v)
        assert not (r > g + 40 and r > b + 40) and not (g > r + 40 and g > b + 40), v


def test_narrow_screen_rules_no_fixed_overflowing_widths_and_amounts_not_truncated():
    rules_only = re.sub(r"@media\s*\([^)]*\)", "", CSS)                                   # 媒体查询条件不是宽度规则
    for m in re.finditer(r"(?<![-\w])(min-)?width:\s*(\d+)px", rules_only):
        assert int(m.group(2)) < 376, f"固定宽度会在 375px 屏溢出：{m.group(0)}"
    assert "text-overflow" not in CSS and "ellipsis" not in CSS, "金额不得被省略号截断"
    assert re.search(r"\.amt\s*\{[^}]*white-space:\s*nowrap", CSS)                       # 金额不换行也不截断
    assert "@media (max-width: 720px)" in CSS
    narrow = CSS[CSS.index("@media (max-width: 720px)"):]
    assert ".tbl thead { display: none; }" in narrow and "attr(data-label)" in narrow     # 窄屏表格折成卡片：不横向溢出
    assert "box-sizing: border-box" in CSS and "max-width: 100%" in CSS
    assert not re.search(r"overflow-x:\s*hidden", CSS), "不得用 overflow-x:hidden 掩盖溢出（会把金额裁掉）"


def test_charts_scale_to_container_and_have_text_alternative():
    assert ".chart svg { display: block; width: 100%; height: auto; max-width: 100%; }" in CSS
    ui = (STATIC / "ui.js").read_text(encoding="utf-8")
    assert "viewBox" in ui and "aria-label" in ui and "clientWidth" in ui
    assert "readout" in ui                                                           # 数值同时以文字读出（不只靠颜色）


def test_every_builtin_view_has_a_panel_registering_its_own_id():
    for vid in BUILTIN:
        t = (registry.BUILTIN_VIEWS_DIR / vid / "panel.js").read_text(encoding="utf-8")
        assert f'MS.registerPanel("{vid}"' in t, vid


def test_panels_use_server_cells_not_client_side_money_math():
    for f in registry.BUILTIN_VIEWS_DIR.glob("*/panel.js"):
        t = f.read_text(encoding="utf-8")
        assert "parseFloat" not in t and "Number(" not in t and "toFixed" not in t, f   # 金额不经 float


def test_static_assets_and_index_are_served(tmp_path):
    from .test_web_fixtures import make_app
    c = make_app(tmp_path).test_client()
    for path in ("/", "/static/app.css", "/static/app.js", "/static/ui.js", "/static/theme.js"):
        assert c.get(path).status_code == 200, path
    assert b"viewport" in c.get("/").data
    assert c.get("/static/../../etc/passwd").status_code in (404, 400)
    assert c.get("/static/ui.js").headers["Cache-Control"] == "no-cache"          # 静态脚本每次校验，避免更新后新旧脚本混用


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="需要 node 做 JS 语法与格式化函数检查")
def test_js_files_parse():
    for f in JS_FILES:
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name}: {r.stderr}"


@pytest.mark.skipif(node is None, reason="需要 node")
def test_client_decimal_formatting_is_string_based_bankers_rounding():
    script = """
    const vm = require('vm'), fs = require('fs');
    const ctx = { window: {}, document: {}, console };
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
    const f = ctx.window.MS.fmtDec, m = ctx.window.MS.fmtMoney;
    const out = [f('1234567.891', 2), f('-80', 2), f('0.125', 2), f('0.135', 2), f('2.675', 2), f('-0.001', 2),
                 f('100000000000000000000.01', 2), f('7.8123456', 4), m('69737.5', 'USD'), m(null, 'USD'), f(null, 2)];
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", script, str(STATIC / "ui.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == '["1,234,567.89","-80.00","0.12","0.14","2.68","0.00","100,000,000,000,000,000,000.01","7.8123","69,737.50 USD","不可用","—"]'


def test_series_colors_for_five_lines_exist_in_every_theme_and_are_not_red_or_green():
    for scope in (":root {", ":root:not([data-theme=\"light\"]) {", ":root[data-theme=\"dark\"] {"):
        b = block(scope)
        for v in ("--s1", "--s2", "--s3", "--s4", "--s5"):
            r, g, bl = var_in(b, v)
            assert not (r > g + 40 and r > bl + 40) and not (g > r + 40 and g > bl + 40), (scope, v)    # 曲线颜色不借用涨跌色


def test_replay_card_columns_collapse_on_narrow_screens_without_fixed_widths():
    assert re.search(r"\.cols\s*\{[^}]*minmax\((\d+)px", CSS) and int(re.search(r"\.cols\s*\{[^}]*minmax\((\d+)px", CSS).group(1)) < 376


def test_ops_panels_never_claim_a_single_win_rate_and_use_only_server_cells():
    for vid in ("tickets", "scoreboard", "replay", "data_status"):
        t = (registry.BUILTIN_VIEWS_DIR / vid / "panel.js").read_text(encoding="utf-8")
        assert "打败" not in t and "win_rate" not in t, vid
        assert "innerHTML" not in t and "Number(" not in t and "parseFloat" not in t and "toFixed" not in t, vid


@pytest.mark.skipif(node is None, reason="需要 node")
def test_table_sorting_and_market_filter_behaviour():
    """表格点击排序（数值按 v、缺失在后、数值列先降序）与美股/港股筛选（无市场归属的行始终显示）；用最小假 DOM 在 node 里实际运行。"""
    script = r"""
    const vm = require('vm'), fs = require('fs');
    class El {
      constructor(t){ this.tag=t; this.children=[]; this.attrs={}; this.listeners={}; this.className=''; this._text=''; this.nodeType=1; this.value=''; this.parentNode=null; }
      setAttribute(k,v){ this.attrs[k]=v; } appendChild(c){ c.parentNode=this; this.children.push(c); return c; }
      addEventListener(e,f){ (this.listeners[e]=this.listeners[e]||[]).push(f); }
      set textContent(v){ this._text=String(v); this.children=[]; } get textContent(){ return this._text + this.children.map(c=>c.textContent).join(''); }
      click(){ (this.listeners.click||[]).forEach(f=>f()); }
      find(pred, out=[]){ if(pred(this)) out.push(this); this.children.forEach(c=>c.find&&c.find(pred,out)); return out; }
    }
    const store = {};
    const ctx = { window: { localStorage: { getItem:k=>store[k]||null, setItem:(k,v)=>{store[k]=v;} } }, console,
      document: { createElement: t => new El(t), createElementNS: (n,t) => new El(t), createTextNode: s => { const e=new El('#text'); e._text=String(s); return e; } } };
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
    const MS = ctx.window.MS;
    const rows = [
      { code: 'US.NVDA', qty: {text:'66', v:'66'}, mv: {text:'15,440.70 USD', v:'15440.7'} },
      { code: 'HK.00700', qty: {text:'11,807', v:'11807'}, mv: {text:'4,994,361.00 HKD', v:'4994361'} },
      { code: 'US.TSLA', qty: {text:'56', v:'56'}, mv: {text:'不可用', na:true, v:null} },
      { code: 'HK.09926', qty: {text:'1,000', v:'1000'}, mv: {text:'106,000.00 HKD', v:'106000'} },
      { code: '汇总', qty: {text:'—', na:true}, mv: {text:'x', na:true} },
    ];
    const cols = [{key:'code',label:'标的'},{key:'qty',label:'数量',num:true},{key:'mv',label:'市值',num:true}];
    const host = MS.table(cols, rows);
    const order = () => host.find(e=>e.tag==='tbody')[0].children.map(tr=>tr.children[0].textContent);
    const th = label => host.find(e=>e.tag==='th').filter(t=>t.textContent.startsWith(label))[0];
    const btn = label => host.find(e=>e.tag==='button').filter(b=>b.textContent===label)[0];
    const out = {};
    out.original = order();
    th('数量').click();  out.qtyDesc = order();                 // 数值列：第一次点＝降序
    th('数量').click();  out.qtyAsc = order();
    th('数量').click();  out.restored = order();                // 第三次：恢复原顺序
    th('市值').click();  out.mvDesc = order();                  // 缺失（不可用）排最后
    th('市值').click();  out.mvAsc = order();                   // 升序时缺失仍在最后
    th('标的').click();  out.codeAsc = order();                 // 文本列：升序
    btn('港股').click(); out.hk = order();                      // 无归属的行（汇总）始终显示
    btn('美股').click(); out.us = order();
    out.stored = store['mystock2.market'];
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", script, str(STATIC / "ui.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    import json
    o = json.loads(r.stdout.strip())
    assert o["original"] == ["US.NVDA", "HK.00700", "US.TSLA", "HK.09926", "汇总"]
    assert o["qtyDesc"] == ["HK.00700", "HK.09926", "US.NVDA", "US.TSLA", "汇总"]          # 缺失（汇总）在最后
    assert o["qtyAsc"] == ["US.TSLA", "US.NVDA", "HK.09926", "HK.00700", "汇总"]
    assert o["restored"] == o["original"]
    assert o["mvDesc"] == ["HK.00700", "HK.09926", "US.NVDA", "US.TSLA", "汇总"]          # 不可用排最后
    assert o["mvAsc"] == ["US.NVDA", "HK.09926", "HK.00700", "US.TSLA", "汇总"]          # 升序时缺失仍在最后
    assert [c for c in o["codeAsc"] if c != "汇总"] == ["HK.00700", "HK.09926", "US.NVDA", "US.TSLA"]       # 文本列升序
    assert set(o["hk"]) == {"HK.00700", "HK.09926", "汇总"} and set(o["us"]) == {"US.NVDA", "US.TSLA", "汇总"}
    assert o["stored"] == "US"


# ---------------------------------------------------------------- M3d：中文名、点击代码弹出详情、买卖标记
FAKE_DOM = r"""
const vm = require('vm'), fs = require('fs');
class El {
  constructor(t){ this.tag=t; this.children=[]; this.attrs={}; this.listeners={}; this.className=''; this._text=''; this.nodeType=1; this.value=''; this.parentNode=null; this.style={}; }
  setAttribute(k,v){ this.attrs[k]=v; } appendChild(c){ c.parentNode=this; this.children.push(c); return c; }
  addEventListener(e,f){ (this.listeners[e]=this.listeners[e]||[]).push(f); }
  set textContent(v){ this._text=String(v); this.children=[]; } get textContent(){ return this._text + this.children.map(c=>c.textContent).join(''); }
  fire(e, ev){ (this.listeners[e]||[]).forEach(f=>f(ev||{})); } click(ev){ this.fire('click', ev); }
  getBoundingClientRect(){ return { left: 0, width: 600 }; }
  find(pred, out=[]){ if(pred(this)) out.push(this); this.children.forEach(c=>c.find&&c.find(pred,out)); return out; }
}
const store = {};
const ctx = { window: { localStorage: { getItem:k=>store[k]||null, setItem:(k,v)=>{store[k]=v;} }, addEventListener(){} }, console,
  document: { createElement: t => new El(t), createElementNS: (n,t) => new El(t), createTextNode: s => { const e=new El('#text'); e._text=String(s); return e; },
              body: { contains: () => true } } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const MS = ctx.window.MS;
"""


@pytest.mark.skipif(node is None, reason="需要 node")
def test_code_column_shows_chinese_name_and_click_opens_stock_detail_without_touching_sort_or_filter():
    script = FAKE_DOM + r"""
    const opened = [];
    MS.openStock = (code) => opened.push(code);
    MS.setNames({ 'US.TSLA': '字典里的名字' });
    const rows = [
      { code: 'US.NVDA', code_name: '合成英伟达', qty: {text:'66', v:'66'} },
      { code: 'HK.00700', code_name: '合成腾讯', qty: {text:'11,807', v:'11807'} },
      { code: 'US.TSLA', qty: {text:'56', v:'56'} },                 // 行里没有 code_name：回退到 MS.setNames 的字典
      { code: 'HK.09926', qty: {text:'1,000', v:'1000'} },           // 没有名称：只显示代码，不报错
      { code: '汇总', qty: {text:'—', na:true} },
    ];
    const host = MS.table([{key:'code',label:'标的'},{key:'qty',label:'数量',num:true}], rows);
    const tds = () => host.find(e=>e.tag==='tbody')[0].children.map(tr=>tr.children[0]);
    const links = () => host.find(e=>e.attrs && e.attrs.role==='button' && e.className==='code-link');
    const out = {};
    out.cells = tds().map(td=>td.textContent);
    out.nLinks = links().length;                                   // 「汇总」不是代码，不可点
    out.names = host.find(e=>e.className && e.className.indexOf('code-name')>=0).map(e=>e.textContent);
    links()[0].click({ stopPropagation(){ out.stopped = true; } });
    links()[1].fire('keydown', { key: 'Enter', preventDefault(){}, stopPropagation(){} });
    links()[2].fire('keydown', { key: 'a', preventDefault(){}, stopPropagation(){} });     // 其他键不触发
    out.opened = opened.slice();
    host.find(e=>e.tag==='th')[1].fire('click');                    // 排序仍只用代码/数值，不受名称影响
    out.afterSort = tds().map(td=>td.textContent);
    host.find(e=>e.tag==='button').filter(b=>b.textContent==='港股')[0].click();     // 市场筛选仍只看 code 前缀
    out.hk = tds().map(td=>td.textContent);
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", script, str(STATIC / "ui.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    import json
    o = json.loads(r.stdout.strip())
    assert o["cells"] == ["US.NVDA合成英伟达", "HK.00700合成腾讯", "US.TSLA字典里的名字", "HK.09926", "汇总"]
    assert o["nLinks"] == 4 and o["names"] == ["合成英伟达", "合成腾讯", "字典里的名字"] and o["stopped"] is True
    assert o["opened"] == ["US.NVDA", "HK.00700"]
    assert o["afterSort"][0].startswith("HK.00700") and o["afterSort"][1].startswith("HK.09926")        # 数量降序：11,807 / 1,000 / 66 / 56
    assert [c[:8] for c in o["hk"] if c != "汇总"] == ["HK.00700", "HK.09926"]


@pytest.mark.skipif(node is None, reason="需要 node")
def test_line_chart_draws_buy_sell_marks_at_trade_price_and_lists_them_in_the_readout():
    script = FAKE_DOM + r"""
    const host = new El('div');
    const xs = ['2026-03-02','2026-03-03','2026-03-04','2026-03-05'];
    MS.lineChart(host, { xs, ccy: 'USD', fmt: v => MS.fmtPx(v), height: 200,
      series: [{ name: '收盘价', color: '#000', points: ['100','110','105','108'].map(y => ({ y })) }],
      marks: [ { x: '2026-03-03', side: 'BUY', y: '105.5', label: 'B 标记说明' }, { x: '2026-03-04', side: 'SELL', y: '107', label: 'S 标记说明' },
               { x: '2099-01-01', side: 'BUY', y: '1', label: '不在图上' }, { x: '2026-03-05', label: '外部资金流说明' } ] });
    const buys = host.find(e=>e.attrs['class']==='mark-buy'), sells = host.find(e=>e.attrs['class']==='mark-sell'), flows = host.find(e=>e.attrs['class']==='flow');
    const readout = host.children[1];
    const hit = host.find(e=>e.tag==='rect' && e.attrs.fill==='transparent')[0];
    hit.fire('mousemove', { clientX: 52 + (600 - 52 - 8) / 3 * 1 });                     // 第 2 个点（03-03）
    const out = { buys: buys.length, sells: sells.length, flows: flows.length, buyTitle: buys[0].children[0].textContent, readout: readout.textContent,
                  px: [MS.fmtPx('105.5'), MS.fmtPx('100'), MS.fmtPx('0.12345'), MS.fmtPx('1234.5')] };
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", script, str(STATIC / "ui.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    import json
    o = json.loads(r.stdout.strip())
    assert (o["buys"], o["sells"], o["flows"]) == (1, 1, 1)                            # 不在 xs 内的标记不画；无 side 的仍是原来的底部小三角
    assert o["buyTitle"] == "B 标记说明" and "B 标记说明" in o["readout"] and "S 标记说明" not in o["readout"]      # 读数只列当日标记
    assert o["px"] == ["105.50", "100.00", "0.1234", "1,234.50"]


def test_stock_modal_contract_in_app_js_and_css():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "MS.openStock = openStock" in app and "/api/v/stock?code=" in app
    assert '"Escape"' in app and "closeStock" in app and "modal-backdrop" in app and 'role: "dialog"' in app and '"aria-modal": "true"' in app
    assert "downOnBackdrop" in app and 'addEventListener("hashchange"' in app          # 点背景可关；换页时关闭
    open_fn = app[app.index("function openStock"): app.index("MS.openStock = openStock")]
    assert "location" not in open_fn and "pushState" not in open_fn, "弹窗不得改变 hash/历史（页面可返回性不变）"
    assert "body.classList.add(\"modal-open\")" in app and "focus()" in app            # 打开时聚焦、关闭后恢复焦点
    assert re.search(r"\.modal-backdrop\s*\{[^}]*position:\s*fixed", CSS)
    narrow = CSS[CSS.index("@media (max-width: 720px) {\n  .modal-backdrop"):]
    assert "height: 100%" in narrow and "border-radius: 0" in narrow and "max-width: none" in narrow   # 窄屏全屏
    assert ".modal-head" in CSS and ".modal-body" in CSS and "overflow-y: auto" in CSS             # 页头（关闭按钮）固定，正文单独滚动


def test_stock_panel_uses_server_cells_and_never_claims_tickets():
    t = (registry.BUILTIN_VIEWS_DIR / "stock" / "panel.js").read_text(encoding="utf-8")
    assert 'MS.registerPanel("stock"' in t and "事后重建" in t and "非前向" in t
    assert "不可用" in t and "意图，不是成交" in t
    assert "ticket" not in t.lower()
    assert "innerHTML" not in t and "Number(" not in t and "parseFloat" not in t and "toFixed" not in t
    mk = CSS[CSS.index(".chart .mark-buy"):]
    assert "var(--s1)" in mk and "var(--s4)" in mk and "var(--up)" not in mk.split(".mk-sell")[0]    # 买卖标记用非红非绿的曲线色


@pytest.mark.skipif(node is None, reason="需要 node")
def test_table_pagination_with_sort_filter_and_row_click():
    script = r"""
    const vm = require('vm'), fs = require('fs');
    class El {
      constructor(t){ this.tag=t; this.children=[]; this.attrs={}; this.listeners={}; this.className=''; this._text=''; this.nodeType=1; this.value=''; this.parentNode=null; }
      setAttribute(k,v){ this.attrs[k]=v; } appendChild(c){ c.parentNode=this; this.children.push(c); return c; }
      addEventListener(e,f){ (this.listeners[e]=this.listeners[e]||[]).push(f); }
      set textContent(v){ this._text=String(v); this.children=[]; } get textContent(){ return this._text + this.children.map(c=>c.textContent).join(''); }
      click(){ (this.listeners.click||[]).forEach(f=>f({target:this})); }
      change(v){ this.value=v; (this.listeners.change||[]).forEach(f=>f()); }
      find(pred, out=[]){ if(pred(this)) out.push(this); this.children.forEach(c=>c.find&&c.find(pred,out)); return out; }
    }
    const store = {};
    const ctx = { window: { localStorage: { getItem:k=>store[k]||null, setItem:(k,v)=>{store[k]=v;} } }, console,
      document: { createElement: t => new El(t), createElementNS: (n,t) => new El(t), createTextNode: s => { const e=new El('#text'); e._text=String(s); return e; } } };
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
    const MS = ctx.window.MS;
    const rows = []; for (let i = 1; i <= 120; i++) rows.push({ code: (i % 2 ? 'US.' : 'HK.') + String(i).padStart(5,'0'), n: {text:String(i), v:String(i)} });
    const clicked = [];
    const host = MS.table([{key:'code',label:'标的'},{key:'n',label:'序号',num:true}], rows, { onRowClick: r => clicked.push(r.code) });
    const body = () => host.find(e=>e.tag==='tbody')[0].children;
    const firstCodes = () => body().map(tr=>tr.children[0].textContent);
    const btn = label => host.find(e=>e.tag==='button').filter(b=>b.textContent===label)[0];
    const pagerText = () => host.find(e=>e.className==='pager')[0].textContent;
    const out = {};
    out.page1 = [body().length, firstCodes()[0], firstCodes()[49]];                 // 默认 50 条/页
    btn('下一页 ›').click(); out.page2 = [body().length, firstCodes()[0]];
    btn('下一页 ›').click(); out.page3 = [body().length, firstCodes()[0]];           // 末页：120−100＝20
    out.nextDisabled = btn('下一页 ›').attrs.disabled === '';
    const size = host.find(e=>e.tag==='select').filter(s=>s.attrs['aria-label']==='每页条数')[0];
    size.change('20'); out.size20 = [body().length, store['mystock2.pageSize']];    // 改每页条数：回到第 1 页并记住
    btn('美股').click(); out.us = [body().length, host.find(e=>e.className==='pager')[0].textContent.includes('共 60 行')];   // 筛选后总数 60，回到第 1 页
    const th = host.find(e=>e.tag==='th').filter(t=>t.textContent.startsWith('序号'))[0];
    th.click(); out.sortedFirst = firstCodes()[0];                                   // 数值列先降序：美股最大序号 119
    body()[0].click(); out.clicked = clicked;                                         // 排序/翻页重绘后点击仍对应当前行
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", script, str(STATIC / "ui.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    import json
    o = json.loads(r.stdout.strip())
    assert o["page1"] == [50, "US.00001", "HK.00050"] and o["page2"] == [50, "US.00051"] and o["page3"] == [20, "US.00101"]
    assert o["nextDisabled"] is True
    assert o["size20"] == [20, "20"]
    assert o["us"] == [20, True]
    assert o["sortedFirst"] == "US.00119" and o["clicked"] == ["US.00119"]
