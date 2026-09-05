/* 应用入口：主页对访客开放，真正写入用户数据的请求仍由后端鉴权。 */
(function () {
  const Yiyu = window.Yiyu;
  let routesRegistered = false;
  let routerStarted = false;

  function boot() {
    Yiyu.theme.init();
    if (Yiyu.editor) Yiyu.editor.init();
    window.addEventListener("yiyu:logout", leaveAccount);
    startApp();
  }

  function startApp() {
    if (!routesRegistered) { Yiyu.router.register("chat", Yiyu.chat); Yiyu.router.register("cognition", Yiyu.cognition); routesRegistered = true; }
    Yiyu.sidebar.render();
    if (!routerStarted) { Yiyu.router.start(); routerStarted = true; }
    else Yiyu.router.resolve();
    if (Yiyu.api.isAuthed()) loadHistory().then(() => Yiyu.sidebar.render());
  }

  function leaveAccount() {
    Yiyu.data.history = [];
    if (location.hash !== "#chat") Yiyu.router.go("chat");
    else Yiyu.router.resolve();
    Yiyu.sidebar.render();
  }

  async function loadHistory() {
    if (!Yiyu.api.isAuthed()) { Yiyu.data.history = []; return; }
    try {
      const items = ((await Yiyu.api.listSessions()) || {}).items || [], now = new Date();
      Yiyu.data.history = items.map((s) => {
        const d = s.updated_at ? new Date(s.updated_at) : now, yesterday = new Date(now);
        yesterday.setDate(now.getDate() - 1);
        const group = d.toDateString() === now.toDateString() ? "今天" : d.toDateString() === yesterday.toDateString() ? "昨天" : "更早";
        return { id: s.session_id, title: s.title || "新对话", group };
      });
    } catch (_) { Yiyu.data.history = []; }
  }

  Yiyu.app = { refreshHistory: () => loadHistory().then(() => Yiyu.sidebar.render()) };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
