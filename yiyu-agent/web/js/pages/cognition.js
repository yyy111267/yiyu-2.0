/* 认知库：按适用范围组织，单列阅读，详情按需展开。 */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});
  const PLUS = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>';
  const SEARCH = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>';
  const CLOSE = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  const SCOPES = [
    { value: "general", label: "通用原则", help: "跨行业、跨公司都适用" },
    { value: "industry", label: "行业认知", help: "研究同一行业时参与召回" },
    { value: "company", label: "公司判断", help: "仅研究该公司时参与召回" },
  ];
  const TOPICS = ["护城河", "估值", "成长", "管理层", "风险", "财务质量", "认知偏误", "自定义"];
  let state = { search: "", scope: "all", sort: "updated", loading: false, loaded: false, error: "", items: [], detailId: "" };

  function refresh() { render(document.getElementById("route-view"), document.getElementById("topbar")); }
  async function loadItems() {
    if (state.loading) return;
    state.loading = true; state.error = "";
    try { const data = await Yiyu.api.listMemory(); state.items = (data.items || []).map(normalizeItem); state.loaded = true; }
    catch (e) { state.error = e.status === 401 ? "需要登录后查看认知库" : "认知库加载失败：" + e.message; }
    finally { state.loading = false; }
  }

  function render(view, topbarEl) {
    view.style.flexDirection = "column";
    topbarEl.innerHTML = `<div class="page-title"><img class="cognition-cat" src="assets/cat.svg" alt=""/><b>认知库</b><span class="sub">你的个人投资方法论</span></div><div class="topbar-right"></div>`;
    if (!Yiyu.api.isAuthed()) {
      view.innerHTML = `<div class="page"><div class="empty-state"><div>登录后查看和管理你的认知库</div><button class="btn btn-primary" id="login-cognition">登录</button></div></div>`;
      document.getElementById("login-cognition").onclick = async () => { if (await Yiyu.api.ensureLogin()) refresh(); };
      return;
    }
    if (!state.loaded && !state.loading) loadItems().then(refresh);
    const enabled = state.items.filter((item) => item.injected).length;
    view.innerHTML = `
      <div class="cognition-page">
        <header class="cognition-head"><div><h1>认知库</h1><p>把判断沉淀成可复用的方法，在相关研究中作为待验证假设参与召回。</p></div><button class="btn btn-primary" id="new-cognition">${PLUS} 新建认知</button></header>
        <div class="cognition-summary"><b>${state.items.length}</b> 条认知 <span>·</span> <b>${enabled}</b> 条参与研究</div>
        <div class="cognition-toolbar">
          <label class="cognition-search">${SEARCH}<input id="cognition-search" value="${escapeHTML(state.search)}" placeholder="搜索标题、内容或行业…" /></label>
          <select id="scope-filter" aria-label="按适用范围筛选"><option value="all">全部范围</option>${SCOPES.map((item) => `<option value="${item.value}">${item.label}</option>`).join("")}</select>
          <select id="sort-filter" aria-label="排序方式"><option value="updated">最近更新</option><option value="scope">按范围</option></select>
        </div>
        <div class="cognition-list" id="cognition-list"></div>
      </div><div id="cognition-drawer"></div>`;
    document.getElementById("new-cognition").onclick = () => openEditor(null);
    document.getElementById("cognition-search").oninput = (e) => { state.search = e.target.value; renderList(); };
    const scope = document.getElementById("scope-filter"); scope.value = state.scope; scope.onchange = (e) => { state.scope = e.target.value; renderList(); };
    const sort = document.getElementById("sort-filter"); sort.value = state.sort; sort.onchange = (e) => { state.sort = e.target.value; renderList(); };
    renderList(); renderDrawer();
  }

  function visibleItems() {
    let list = state.items.filter((item) => state.scope === "all" || item.subjectScope === state.scope);
    const query = state.search.trim().toLowerCase();
    if (query) list = list.filter((item) => [item.title, item.content, item.category, item.target].join(" ").toLowerCase().includes(query));
    return [...list].sort(state.sort === "scope" ? (a, b) => scopeIndex(a.subjectScope) - scopeIndex(b.subjectScope) || b.updatedAt.localeCompare(a.updatedAt) : (a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }

  function renderList() {
    const root = document.getElementById("cognition-list"); if (!root) return;
    if (state.loading) { root.innerHTML = `<div class="cognition-empty">正在加载…</div>`; return; }
    if (state.error) { root.innerHTML = `<div class="cognition-empty">${escapeHTML(state.error)}<button class="btn btn-ghost btn-sm" id="retry-cognition">重试</button></div>`; document.getElementById("retry-cognition").onclick = () => { state.loaded = false; loadItems().then(refresh); }; return; }
    const list = visibleItems();
    if (!list.length) { root.innerHTML = `<div class="cognition-empty">没有匹配的认知。换个范围，或新建第一条。</div>`; return; }
    root.innerHTML = list.map((item) => `
      <article class="cognition-row ${item.injected ? "" : "muted"}" data-id="${item.id}" tabindex="0">
        <div><div class="cognition-labels"><span class="scope-badge scope-${item.subjectScope}">${scopeLabel(item.subjectScope)}</span>${item.target ? `<span>${escapeHTML(item.target)}</span>` : ""}${item.category ? `<span>${escapeHTML(item.category)}</span>` : ""}</div><h2>${escapeHTML(item.title)}</h2><p>${escapeHTML(summary(item.content))}</p></div>
        <div class="cognition-row-side"><span>${item.updatedAt}</span><span>${item.injected ? "参与研究" : "已暂停"}</span><span class="row-arrow">→</span></div>
      </article>`).join("");
    root.querySelectorAll(".cognition-row").forEach((row) => {
      const open = () => { state.detailId = row.dataset.id; renderDrawer(); };
      row.onclick = open; row.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") open(); };
    });
  }

  function renderDrawer() {
    const root = document.getElementById("cognition-drawer"); if (!root) return;
    const item = state.items.find((entry) => entry.id === state.detailId);
    if (!item) { root.innerHTML = ""; return; }
    root.innerHTML = `<div class="drawer-backdrop" id="drawer-backdrop"></div><aside class="cognition-drawer" role="dialog" aria-label="认知详情">
      <div class="drawer-head"><span>认知详情</span><button class="btn-icon" id="drawer-close" aria-label="关闭">${CLOSE}</button></div>
      <div class="drawer-body"><div class="cognition-labels"><span class="scope-badge scope-${item.subjectScope}">${scopeLabel(item.subjectScope)}</span>${item.target ? `<span>${escapeHTML(item.target)}</span>` : ""}${item.category ? `<span>${escapeHTML(item.category)}</span>` : ""}</div><h2>${escapeHTML(item.title)}</h2><div class="drawer-section"><label>判断说明</label><p>${escapeHTML(item.content || "暂无详细说明").replace(/\n/g, "<br>")}</p></div><div class="drawer-section"><label>研究使用</label><p>${scopeUsage(item)}</p></div><div class="drawer-meta">${item.source === "ai" ? "AI 蒸馏" : "手动创建"} · 更新于 ${item.updatedAt}</div></div>
      <div class="drawer-foot"><label class="inject-control"><span class="switch"><input type="checkbox" id="drawer-inject" ${item.injected ? "checked" : ""}><span class="slider"></span></span>参与研究</label><div><button class="btn btn-ghost" id="drawer-delete">删除</button><button class="btn btn-primary" id="drawer-edit">编辑</button></div></div>
    </aside>`;
    const close = () => { state.detailId = ""; renderDrawer(); };
    document.getElementById("drawer-close").onclick = close; document.getElementById("drawer-backdrop").onclick = close;
    document.getElementById("drawer-edit").onclick = () => openEditor(item); document.getElementById("drawer-delete").onclick = () => confirmDelete(item);
    document.getElementById("drawer-inject").onchange = async (e) => {
      e.target.disabled = true;
      try { const updated = await Yiyu.api.updateMemory(item.id, { status: e.target.checked ? "active" : "archived" }); replaceItem(updated.item); renderList(); renderDrawer(); }
      catch (error) { e.target.checked = !e.target.checked; Yiyu.modal.toast("保存失败：" + error.message, "danger"); }
    };
  }

  function openEditor(item) {
    const draft = item ? { ...item } : { title: "", content: "", category: "护城河", subjectScope: "general", target: "", injected: true };
    Yiyu.modal.open({
      title: item ? "编辑认知" : "新建认知",
      bodyHTML: `<div class="field"><label>核心判断 *</label><input class="input" id="f-title" maxlength="40" value="${escapeHTML(draft.title)}" placeholder="用一句话写清结论（40 字以内）" /></div>
        <div class="field"><label>适用范围 *</label><select class="input" id="f-scope">${SCOPES.map((scope) => `<option value="${scope.value}">${scope.label} · ${scope.help}</option>`).join("")}</select></div>
        <div class="field" id="f-target-wrap"><label id="f-target-label"></label><input class="input" id="f-target" value="${escapeHTML(draft.target)}" /></div>
        <div class="field"><label>主题</label><select class="input" id="f-category">${TOPICS.map((topic) => `<option value="${topic}">${topic}</option>`).join("")}</select></div>
        <div class="field"><label>判断说明 *</label><textarea class="input textarea cognition-editor-content" id="f-content" placeholder="适用条件：…\n判断逻辑：…\n证伪条件：…">${escapeHTML(draft.content)}</textarea><small>通用原则与行业认知需写明“适用条件”和“证伪条件”。</small></div>
        <label class="inject-control"><span class="switch"><input type="checkbox" id="f-inject" ${draft.injected ? "checked" : ""}><span class="slider"></span></span>参与相关研究</label>`,
      footerHTML: `<button class="btn btn-ghost" id="f-cancel">取消</button><button class="btn btn-primary" id="f-save">${item ? "保存" : "创建"}</button>`,
      onMount(root) {
        const scope = root.querySelector("#f-scope"); scope.value = draft.subjectScope;
        const category = root.querySelector("#f-category"); category.value = TOPICS.includes(draft.category) ? draft.category : "自定义";
        const updateTarget = () => { const wrap = root.querySelector("#f-target-wrap"); wrap.hidden = scope.value === "general"; root.querySelector("#f-target-label").textContent = scope.value === "industry" ? "行业名称 *" : "证券代码 *"; root.querySelector("#f-target").placeholder = scope.value === "industry" ? "例如：白酒" : "例如：600519.SH"; };
        updateTarget(); scope.onchange = updateTarget; root.querySelector("#f-cancel").onclick = Yiyu.modal.close;
        root.querySelector("#f-save").onclick = async () => {
          const next = { title: root.querySelector("#f-title").value.trim(), content: root.querySelector("#f-content").value.trim(), category: category.value, subjectScope: scope.value, target: root.querySelector("#f-target").value.trim(), injected: root.querySelector("#f-inject").checked };
          if (!next.title || !next.content) return Yiyu.modal.toast("请填写核心判断和判断说明", "danger");
          if (next.subjectScope !== "company" && (!next.content.includes("适用条件") || !next.content.includes("证伪条件"))) return Yiyu.modal.toast("请补充适用条件和证伪条件", "danger");
          if (next.subjectScope !== "general" && !next.target) return Yiyu.modal.toast(next.subjectScope === "industry" ? "请填写行业名称" : "请填写证券代码", "danger");
          const button = root.querySelector("#f-save"); button.disabled = true;
          try {
            if (item) { const updated = await Yiyu.api.updateMemory(item.id, toApiFields(next)); replaceItem(updated.item); }
            else { const response = await Yiyu.api.confirmMemory([{ card_id: "manual:" + Date.now(), action: "confirm", candidate: toCandidate(next) }], "manual"); const saved = (response.results || []).find((result) => result.saved); if (!saved) throw new Error(response.results?.[0]?.error || "创建失败"); if (!next.injected) await Yiyu.api.updateMemory(saved.id, { status: "archived" }); state.loaded = false; await loadItems(); }
            Yiyu.modal.close(); state.detailId = item ? item.id : ""; refresh(); Yiyu.modal.toast(item ? "已保存" : "已创建", "success");
          } catch (error) { Yiyu.modal.toast("保存失败：" + error.message, "danger"); }
          finally { button.disabled = false; }
        };
      },
    });
  }

  function confirmDelete(item) {
    Yiyu.modal.open({ title: "删除认知", bodyHTML: `<p>确定删除“${escapeHTML(item.title)}”吗？</p>`, footerHTML: `<button class="btn btn-ghost" id="delete-cancel">取消</button><button class="btn btn-primary" id="delete-confirm">删除</button>`, onMount(root) {
      root.querySelector("#delete-cancel").onclick = Yiyu.modal.close;
      root.querySelector("#delete-confirm").onclick = async () => { try { await Yiyu.api.deleteMemory(item.id); state.items = state.items.filter((entry) => entry.id !== item.id); state.detailId = ""; Yiyu.modal.close(); refresh(); } catch (error) { Yiyu.modal.toast("删除失败：" + error.message, "danger"); } };
    } });
  }

  function normalizeItem(raw) {
    const legacyIndustry = raw.category === "行业认知";
    const subjectScope = raw.subject_scope || (legacyIndustry ? "industry" : "general");
    return { raw, id: raw.id, title: raw.statement || "", content: raw.content || raw.basis || "", category: legacyIndustry ? "自定义" : (raw.category || "自定义"), subjectScope, target: subjectScope === "company" ? (raw.symbol || "") : subjectScope === "industry" ? (raw.scope || "") : "", injected: raw.status === "active" || raw.status === "confirmed", source: raw.owner === "agent" || raw.source === "research_extract" ? "ai" : "manual", updatedAt: dateOnly(raw.updated_at || raw.first_seen_at) };
  }
  function replaceItem(raw) { const item = normalizeItem(raw); const index = state.items.findIndex((entry) => entry.id === item.id); if (index >= 0) state.items[index] = item; else state.items.unshift(item); }
  function toApiFields(item) { return { statement: item.title, content: item.content, category: item.category, subject_scope: item.subjectScope, scope: item.subjectScope === "industry" ? item.target : "", symbol: item.subjectScope === "company" ? item.target : "", status: item.injected ? "active" : "archived" }; }
  function toCandidate(item) { return { ...toApiFields(item), type: "cognition" }; }
  function scopeLabel(value) { return SCOPES.find((item) => item.value === value)?.label || "通用原则"; }
  function scopeIndex(value) { return SCOPES.findIndex((item) => item.value === value); }
  function scopeUsage(item) { if (!item.injected) return "当前已暂停，不会参与研究。"; if (item.subjectScope === "industry") return `研究“${escapeHTML(item.target)}”相关公司时优先召回，并作为待验证假设。`; if (item.subjectScope === "company") return `仅研究“${escapeHTML(item.target)}”时召回，并转成复核问题。`; return "研究主题相关时召回，并作为待验证假设。"; }
  function summary(value) { const text = String(value || "暂无详细说明").replace(/\s+/g, " "); return text.length > 92 ? text.slice(0, 92) + "…" : text; }
  function dateOnly(value) { return value ? String(value).slice(0, 10) : new Date().toISOString().slice(0, 10); }
  function escapeHTML(value) { return String(value || "").replace(/[&<>\"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char])); }
  Yiyu.cognition = { render };
})();
