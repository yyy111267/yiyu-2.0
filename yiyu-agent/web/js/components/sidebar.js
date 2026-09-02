/* ============================================================
   侧边栏组件 (window.Yiyu.sidebar)
   精简版：仅保留 新对话 / 认知库管理 / 历史对话
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});

  const ICONS = {
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
    book: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
    logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>',
    /* 折叠按钮：双竖线 · 极简非 AI 感 */
    panel: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="7" y1="3" x2="7" y2="17"/><line x1="13" y1="3" x2="13" y2="17"/></svg>',
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'
  };

  /* MVP 收口：第一版主导航只保留 公司研究 / 我的认知 / 历史研究 / 设置(账户)。
     持仓追踪、交易复盘完全隐藏，不进主导航、不伪装成已上线功能。 */
  const NAV = [
    { id: "chat", label: "公司研究", icon: ICONS.search },
    { id: "cognition", label: "我的认知", icon: ICONS.book },
    { id: "history", label: "历史研究", icon: ICONS.clock },
  ];

  /* 真实登录账号：昵称可自定义（本地偏好），邮箱来自登录态 */
  function currentUser() {
    const email = (Yiyu.api.getEmail() || "").trim();
    const nick = Yiyu.store.get("nickname");
    const name = nick || (email ? email.split("@")[0] : "用户");
    return { email, name };
  }

  function escapeHTML(s) { return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  function render() {
    const el = document.getElementById("sidebar");
    const collapsed = Yiyu.store.get("sidebarCollapsed");
    const hist = Yiyu.data.history;  // 由 app.js 从 /sessions 真实加载（替换 mock）
    const me = currentUser();

    const groups = {};
    hist.forEach((h) => { (groups[h.group] = groups[h.group] || []).push(h); });

    el.className = "sidebar" + (collapsed ? " collapsed" : "");
    el.innerHTML = `
      <button class="side-collapse" id="side-collapse" title="折叠/展开侧边栏">${ICONS.panel}</button>
      <div class="side-logo">
        <img src="assets/logo-cat.png" alt="以渔" data-edit-id="side-logo" data-edit-type="icon" data-edit-label="侧边栏 Logo" style="border-radius:14px;object-fit:cover;" onerror="this.style.display='none'"/>
        <div class="brand"><b data-edit-id="brand-name" data-edit-label="品牌名">以渔</b><span data-edit-id="brand-sub" data-edit-label="品牌副标题">投研认知陪练</span></div>
      </div>
      <div class="side-newchat"><button class="btn btn-primary" id="new-chat">${ICONS.plus}<span data-edit-id="newchat-label" data-edit-label="新对话按钮文字">新对话</span></button></div>
      <nav class="side-nav">
        ${NAV.map((n) => `<div class="nav-item" data-route="${n.id}">${n.icon}<span class="label" data-edit-id="nav-${n.id}" data-edit-label="${n.label}导航">${n.label}</span></div>`).join("")}
      </nav>
      <div class="side-history">
        <div class="side-section-label">历史对话</div>
        ${hist.length
          ? Object.keys(groups).map((g) => `
          <div class="history-group-title">${g}</div>
          ${groups[g].map((h) => `<div class="history-item" data-hid="${escapeHTML(h.id)}"><span class="dot"></span><span class="title">${escapeHTML(h.title)}</span></div>`).join("")}
        `).join("")
          : `<div class="side-empty fs-xs t-muted">还没有历史研究，发起一次公司研究后即会出现在这里。</div>`}
      </div>
      <div class="side-account">
        <div class="account-card" id="account-settings" title="个人设置">
          <div class="avatar-sm">${escapeHTML(me.name.charAt(0))}</div>
          <div class="meta"><b data-edit-id="account-name" data-edit-label="账户名">${escapeHTML(me.name)}</b><span data-edit-id="account-settings-label" data-edit-label="个人设置文字">个人设置</span></div>
          ${ICONS.settings}
        </div>
      </div>
    `;

    el.querySelectorAll(".nav-item").forEach((item) => {
      const route = item.dataset.route;
      if (route === "history") {
        // 历史研究：聚焦侧边栏已有历史列表（数据来自后端会话），不新建页面、不伪装半成品
        item.addEventListener("click", () => {
          const hist = document.querySelector(".side-history");
          if (hist) hist.scrollIntoView({ behavior: "smooth", block: "start" });
        });
      } else {
        item.addEventListener("click", () => Yiyu.router.go(route));
      }
    });
    el.querySelectorAll(".history-item").forEach((item) => {
      item.addEventListener("click", () => {
        const target = "chat?sid=" + encodeURIComponent(item.dataset.hid);
        // 重复点击当前已打开的那一条：hash 不变不会触发 hashchange，必须手动重新渲染
        if (location.hash === "#" + target) Yiyu.router.resolve();
        else Yiyu.router.go(target);
      });
    });
    document.getElementById("new-chat").addEventListener("click", () => {
      Yiyu.router.go("chat");  // 不带 sid = 新会话
      if (Yiyu.chat && Yiyu.chat.reset) Yiyu.chat.reset();
    });
    document.getElementById("side-collapse").addEventListener("click", () => {
      const c = !Yiyu.store.get("sidebarCollapsed");
      Yiyu.store.set("sidebarCollapsed", c);
      render();
    });
    /* 个人设置点击 — 真实登录账号信息 + 退出登录 */
    document.getElementById("account-settings").addEventListener("click", () => {
      const me = currentUser();
      Yiyu.modal.open({
        title: "个人设置",
        bodyHTML: `
          <div class="field"><label>昵称</label><input class="input" id="set-name" value="${escapeHTML(me.name)}"/></div>
          <div class="field" style="display:flex;align-items:center;justify-content:space-between;">
            <label>主题</label>
            <button class="btn-icon" id="theme-toggle" title="切换浅色/深色模式" style="width:34px;height:34px;border-radius:50%;"></button>
          </div>
          <div class="field"><label>邮箱（登录账号）</label><input class="input" id="set-email" value="${escapeHTML(me.email)}" type="email" disabled style="opacity:.6"/></div>
          <div class="fs-xs t-muted">邮箱即登录账号，验证码登录；认知库与该账号绑定，换设备登录后数据不丢。</div>
        `,
        footerHTML: `<button class="btn btn-ghost" id="set-logout">${ICONS.logout} 退出登录</button>
                     <span style="flex:1"></span>
                     <button class="btn btn-ghost" id="set-cancel">取消</button>
                     <button class="btn btn-accent" id="set-save">保存</button>`,
        onMount(root) {
          const tt = root.querySelector("#theme-toggle");
          if (tt) { tt.innerHTML = Yiyu.theme.buttonHTML(); tt.onclick = Yiyu.theme.toggle; }
          root.querySelector("#set-cancel").onclick = Yiyu.modal.close;
          root.querySelector("#set-save").onclick = () => {
            const nick = root.querySelector("#set-name").value.trim();
            Yiyu.store.set("nickname", nick);
            Yiyu.modal.close(); Yiyu.modal.toast("设置已保存", "success"); render();
          };
          root.querySelector("#set-logout").onclick = () => {
            Yiyu.modal.close();
            Yiyu.api.logout();  /* 派发 yiyu:logout → app.js 回登录门禁 */
          };
        }
      });
    });
    /* 重新套用编辑器覆盖（折叠/重渲染后保持用户修改） */
    if (Yiyu.editor && Yiyu.editor.applyOverrides) Yiyu.editor.applyOverrides(el);
  }

  Yiyu.sidebar = { render, setActive(route) {
    document.querySelectorAll(".nav-item").forEach((n) => {
      n.classList.toggle("active", n.dataset.route === route);
    });
  } };
})();
