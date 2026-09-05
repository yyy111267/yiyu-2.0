/* ============================================================
   主题切换 (window.Yiyu.theme)
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});

  const SUN = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  const MOON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    Yiyu.store.set("theme", theme);
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.innerHTML = theme === "dark" ? SUN : MOON;
  }
  function toggle() {
    const cur = document.documentElement.getAttribute("data-theme");
    apply(cur === "dark" ? "light" : "dark");
  }
  function init() {
    /* 当前视觉只维护一套 Kimi 式浅色灰阶主题。 */
    apply("light");
  }
  Yiyu.theme = { init, toggle, buttonHTML: () => MOON };
})();
