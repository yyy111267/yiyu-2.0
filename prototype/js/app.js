/* ============================================================
   应用入口
   门禁流程：未登录 → 全屏登录页；登录成功 → 拉起主应用。
   会话中 401（token 过期）→ api.logout() 派发 yiyu:logout → 回到登录页。
   ============================================================ */
(function () {
  const Yiyu = window.Yiyu;

  let routesRegistered = false;
  let routerStarted = false;

  function boot() {
    Yiyu.theme.init();
    if (Yiyu.editor) Yiyu.editor.init();

    /* token 失效/登出 → 回到登录门禁 */
    window.addEventListener("yiyu:logout", showLoginGate);

    if (!Yiyu.api.isAuthed()) { showLoginGate(); return; }
    startApp();
  }

  /* 全屏登录页 */
  function showLoginGate() {
    document.body.classList.add("pre-auth");
    Yiyu.login.render({ onSuccess: enterApp });
  }

  /* 登录成功 → 进入主应用 */
  function enterApp() {
    document.body.classList.remove("pre-auth");
    startApp();
  }

  function startApp() {
    Yiyu.sidebar.render();

    if (!routesRegistered) {
      /* MVP 收口：第一版只开放 公司研究 / 我的认知 / 历史研究 / 设置。
         持仓追踪、交易复盘完全隐藏（内部测试版），不注册路由、不进主导航。 */
      Yiyu.router.register("chat", Yiyu.chat);
      Yiyu.router.register("cognition", Yiyu.cognition);
      routesRegistered = true;
    }
    if (!routerStarted) {
      Yiyu.router.start();
      routerStarted = true;
    } else {
      /* 过期重登：按当前 hash 重新渲染当前页 */
      const cur = location.hash.replace("#", "") || "chat";
      Yiyu.router.go(cur.split("?")[0]);
    }

    loadHistory().then(() => Yiyu.sidebar.render());
  }

  /* 拉取真实历史会话并映射成 sidebar 期望结构。
     标题直接取后端返回的 title（= 首条 query 的清洗截断结果），
     转义交给 sidebar 渲染时统一处理，这里只管数据。 */
  async function loadHistory() {
    try {
      const r = await Yiyu.api.listSessions();
      const items = (r && r.items) || [];
      const now = new Date();
      Yiyu.data.history = items.map((s) => {
        const d = s.updated_at ? new Date(s.updated_at) : now;
        const sameDay = d.toDateString() === now.toDateString();
        const yest = new Date(now); yest.setDate(now.getDate() - 1);
        const isYest = d.toDateString() === yest.toDateString();
        const group = sameDay ? "今天" : isYest ? "昨天" : "更早";
        const time = d.getHours().toString().padStart(2, "0") + ":" + d.getMinutes().toString().padStart(2, "0");
        return { id: s.session_id, title: s.title || "新对话", group, time };
      });
    } catch (e) {
      /* 未登录或接口不可用时保留原 mock，避免白屏 */
      Yiyu.data.history = (Yiyu.data && Yiyu.data.history) || [];
    }
  }

  Yiyu.app = {
    /* 研究结束后刷新侧边栏（新会话要出现在历史里） */
    refreshHistory() { return loadHistory().then(() => Yiyu.sidebar.render()); },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
