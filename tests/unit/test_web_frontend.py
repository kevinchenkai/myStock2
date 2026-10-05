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
BUILTIN = ["account_overview", "holdings", "trades", "pnl", "equity_trend", "fx"]


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
