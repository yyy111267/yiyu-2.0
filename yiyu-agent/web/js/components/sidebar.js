/* 侧边栏：访客可浏览主页；个人数据入口在登录后显示。 */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});
  const ICONS = {
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>',
    book: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
    panel: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8"><line x1="7" y1="3" x2="7" y2="17"/><line x1="13" y1="3" x2="13" y2="17"/></svg>',
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3m10-10h-3M5 12H2m17.1-7.1-2.1 2.1M7 17l-2.1 2.1m14.2 0L17 17M7 7 4.9 4.9"/></svg>',
  };
  const NAV = [{ id: "chat", label: "公司研究", icon: ICONS.search }, { id: "cognition", label: "我的认知", icon: ICONS.book }];
  const escapeHTML = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const currentUser = () => { const email = Yiyu.api.getEmail(); return { email, name: Yiyu.store.get("nickname") || (email ? email.split("@")[0] : "") }; };

  function render() {
    const el = document.getElementById("sidebar"), collapsed = Yiyu.store.get("sidebarCollapsed"), authed = Yiyu.api.isAuthed(), me = currentUser();
    const groups = {}; (Yiyu.data.history || []).forEach((h) => (groups[h.group] = groups[h.group] || []).push(h));
    el.className = "sidebar" + (collapsed ? " collapsed" : "");
    el.innerHTML = `
      <button class="side-collapse" id="side-collapse" aria-label="折叠或展开侧边栏">${ICONS.panel}</button>
      <div class="side-logo"><img src="assets/logo.svg" alt="以渔"/><div class="brand"><b>以渔</b><span>投研认知陪练</span></div></div>
      <div class="side-newchat"><button class="btn" id="new-chat">${ICONS.plus}<span>新对话</span></button></div>
      <nav class="side-nav">${NAV.map((n) => `<button class="nav-item" data-route="${n.id}">${n.icon}<span class="label">${n.label}</span></button>`).join("")}</nav>
      ${authed ? `<div class="side-history"><div class="side-section-label">历史对话</div>${Object.keys(groups).length ? Object.keys(groups).map((g) => `<div class="history-group-title">${g}</div>${groups[g].map((h) => `<button class="history-item" data-hid="${escapeHTML(h.id)}"><span class="dot"></span><span class="title">${escapeHTML(h.title)}</span></button>`).join("")}`).join("") : `<div class="side-empty">还没有历史研究</div>`}</div>` : `<div class="side-guest-note">登录后可保存研究历史和认知。</div>`}
      <div class="side-account">${authed ? `<button class="account-card" id="account-settings"><span class="avatar-sm">${escapeHTML(me.name.charAt(0))}</span><span class="meta"><b>${escapeHTML(me.name)}</b><span>个人设置</span></span>${ICONS.settings}</button>` : `<button class="account-card" id="account-login"><img class="account-logo" src="assets/logo.svg" alt=""/><span class="meta"><b>登录</b><span>保存你的研究</span></span></button>`}</div>`;
    el.querySelector("#new-chat").onclick = () => { Yiyu.router.go("chat"); if (Yiyu.chat.reset) Yiyu.chat.reset(); };
    el.querySelector("#side-collapse").onclick = () => { Yiyu.store.set("sidebarCollapsed", !collapsed); render(); };
    el.querySelectorAll(".nav-item").forEach((item) => item.onclick = async () => { if (item.dataset.route === "cognition" && !authed) return Yiyu.login.open(); Yiyu.router.go(item.dataset.route); });
    el.querySelectorAll(".history-item").forEach((item) => item.onclick = () => Yiyu.router.go("chat?sid=" + encodeURIComponent(item.dataset.hid)));
    const login = el.querySelector("#account-login"); if (login) login.onclick = async () => { if (await Yiyu.login.open()) { Yiyu.sidebar.render(); if (Yiyu.app) Yiyu.app.refreshHistory(); } };
    const settings = el.querySelector("#account-settings"); if (settings) settings.onclick = () => Yiyu.modal.open({ title: "个人设置", bodyHTML: `<div class="field"><label>昵称</label><input class="input" id="set-name" value="${escapeHTML(me.name)}"/></div><div class="field"><label>邮箱</label><input class="input" value="${escapeHTML(me.email)}" disabled/></div>`, footerHTML: `<button class="btn btn-ghost" id="set-logout">退出登录</button><button class="btn btn-primary" id="set-save">保存</button>`, onMount(root) { root.querySelector("#set-save").onclick = () => { Yiyu.store.set("nickname", root.querySelector("#set-name").value.trim()); Yiyu.modal.close(); render(); }; root.querySelector("#set-logout").onclick = () => { Yiyu.modal.close(); Yiyu.api.logout(); }; } });
  }
  Yiyu.sidebar = { render, setActive(route) { document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.route === route)); } };
})();
