/* ============================================================
   Hash 路由 (window.Yiyu.router)
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});

  const registry = {};
  let current = null;

  const router = {
    register(route, pageObj) { registry[route] = pageObj; },
    start() {
      window.addEventListener("hashchange", () => this.resolve());
      if (!location.hash) location.hash = "#chat";
      else this.resolve();
    },
    resolve() {
      const raw = location.hash.replace("#", "") || "chat";
      const route = raw.split("?")[0] || "chat";  // 剥离 query（如 chat?sid=xxx）
      const page = registry[route] || registry["chat"];
      const view = document.getElementById("route-view");
      view.innerHTML = "";
      const topbar = document.getElementById("topbar");
      topbar.innerHTML = "";
      current = page;
      if (page && page.render) page.render(view, topbar);
      // 更新侧边栏 active
      if (Yiyu.sidebar && Yiyu.sidebar.setActive) Yiyu.sidebar.setActive(route);
      // 套用可视化编辑器保存的覆盖
      if (Yiyu.editor && Yiyu.editor.applyOverrides) Yiyu.editor.applyOverrides(document);
      // 滚到顶部
      requestAnimationFrame(() => { view.scrollTop = 0; });
    },
    go(route) { location.hash = "#" + route; }
  };

  Yiyu.router = router;
})();
