/* 主题：跟随系统（auto），可手动切换为 light / dark，选择记在 localStorage（读写都容错：隐私模式下不可用时仍能工作）。 */
(function () {
  var KEY = "mystock2.theme";
  var ORDER = ["auto", "light", "dark"];
  var LABEL = { auto: "主题：跟随系统", light: "主题：浅色", dark: "主题：深色" };
  function read() {
    try { var v = window.localStorage.getItem(KEY); return ORDER.indexOf(v) >= 0 ? v : "auto"; } catch (e) { return "auto"; }
  }
  function write(v) { try { window.localStorage.setItem(KEY, v); } catch (e) { /* 忽略 */ } }
  function apply(v) {
    var root = document.documentElement;
    if (v === "light" || v === "dark") { root.setAttribute("data-theme", v); } else { root.removeAttribute("data-theme"); }
    var btn = document.getElementById("theme-btn");
    if (btn) { btn.textContent = LABEL[v]; btn.setAttribute("aria-label", LABEL[v] + "（点击切换）"); }
  }
  var current = read();
  apply(current);
  window.MSTheme = {
    cycle: function () { current = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length]; write(current); apply(current); return current; },
    current: function () { return current; },
    refresh: function () { apply(current); }
  };
})();
