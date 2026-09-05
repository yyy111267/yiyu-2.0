/* ============================================================
   认知库管理页 (window.Yiyu.cognition)
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});

  const PLUS = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>';
  const SEARCH = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>';
  const MORE = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>';
  const ARROW = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
  const TRASH = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>';
  const EDIT = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';

  const TYPES = ["选股标准", "卖出纪律", "仓位原则", "能力圈", "行业认知", "自定义"];
  const TYPE_COLOR = {
    "选股标准": "chip-indigo", "卖出纪律": "chip-red", "仓位原则": "chip-cyan",
    "能力圈": "chip-accent", "行业认知": "chip-green", "自定义": "chip"
  };

  let state = {
    filter: "全部", search: "", sort: "最新", batch: false, selected: new Set(),
    loading: false, loaded: false, error: "", items: [],
  };

  function getList() { return state.items; }
  function refresh() { const v = document.getElementById("route-view"); const t = document.getElementById("topbar"); render(v, t); }

  async function loadItems() {
    if (state.loading) return;
    state.loading = true;
    state.error = "";
    try {
      const data = await Yiyu.api.listMemory();
      state.items = (data.items || []).map(normalizeItem);
      state.loaded = true;
    } catch (e) {
      state.error = e.status === 401 ? "需要登录后查看认知库" : "认知库加载失败：" + e.message;
      state.items = [];
    } finally {
      state.loading = false;
    }
  }

  function stats() {
    const list = getList();
    const now = new Date();
    const weekAgo = new Date(now - 7 * 864e5);
    const thisWeek = list.filter((c) => new Date(c.createdAt) >= weekAgo).length;
    const injected = list.filter((c) => c.injected).length;
    const tags = new Set(); list.forEach((c) => (c.tags || []).forEach((t) => tags.add(t)));
    return { total: list.length, thisWeek, injected, coverage: tags.size };
  }

  function topbar(topbarEl) {
    topbarEl.innerHTML = `
      <div class="page-title"><b data-edit-id="cog-top-title" data-edit-label="认知库页标题">认知库</b><span class="sub" data-edit-id="cog-top-sub" data-edit-label="认知库页副标题">你的个人投资方法论资产</span></div>
      <div class="topbar-right"></div>`;
  }

  function render(view, topbarEl) {
    view.style.flexDirection = "column";
    topbar(topbarEl);
    if (!Yiyu.api.isAuthed()) {
      view.innerHTML = `
        <div class="page">
          <div class="empty-state">
            <div class="emoji">🔐</div>
            <div>登录后查看和管理你的认知库</div>
            <button class="btn btn-primary" id="login-cognition">登录</button>
          </div>
        </div>`;
      document.getElementById("login-cognition").addEventListener("click", async () => {
        if (await Yiyu.api.ensureLogin()) refresh();
      });
      return;
    }
    if (!state.loaded && !state.loading) {
      loadItems().then(refresh);
    }
    const s = stats();
    view.innerHTML = `
      <div class="page">
        <div class="page-header">
          <div class="title-block"><h1 data-edit-id="cog-page-title" data-edit-label="认知库大标题">认知库</h1><p data-edit-id="cog-page-desc" data-edit-label="认知库说明">管理你的投资认知，它们会在你研究公司时自动作为上下文注入。</p></div>
          <button class="btn btn-primary" id="new-cog">${PLUS} 新建认知</button>
        </div>

        <div class="stat-row">
          <div class="stat-card card"><div class="icon icon-indigo">${PLUS}</div><div class="val">${s.total}</div><div class="lbl" data-edit-id="stat-total" data-edit-label="统计-总数">认知条目总数</div><div class="deco">库</div></div>
          <div class="stat-card card"><div class="icon icon-accent">📈</div><div class="val">${s.thisWeek}</div><div class="lbl" data-edit-id="stat-week" data-edit-label="统计-本周">本周新增</div><div class="deco">+</div></div>
          <div class="stat-card card"><div class="icon icon-green">🔗</div><div class="val">${s.injected}</div><div class="lbl" data-edit-id="stat-inject" data-edit-label="统计-注入">已启用注入</div><div class="deco">↪</div></div>
          <div class="stat-card card"><div class="icon icon-cyan">🏷️</div><div class="val">${s.coverage}</div><div class="lbl" data-edit-id="stat-cover" data-edit-label="统计-标签">覆盖标签数</div><div class="deco">#</div></div>
        </div>

        <div class="cog-toolbar">
          <div class="cog-search">${SEARCH}<input class="input" id="cog-search" placeholder="搜索认知标题或内容…" value="${state.search}"/></div>
          <div class="filter-tags" id="filter-tags">
            ${["全部", ...TYPES].map((t) => `<span class="tag-opt ${state.filter === t ? "active" : ""}" data-f="${t}">${t}</span>`).join("")}
          </div>
          <select class="sort-select" id="sort-select">
            <option value="最新">最新优先</option><option value="频次">使用频率</option><option value="标签">关联标签</option>
          </select>
          <label class="batch-toggle"><span class="switch"><input type="checkbox" id="batch-toggle" ${state.batch ? "checked" : ""}><span class="slider"></span></span>批量管理</label>
        </div>

        <div class="cog-grid" id="cog-grid"></div>

        ${injectDemoHTML()}
      </div>`;

    document.getElementById("new-cog").addEventListener("click", () => openEditor(null));
    const search = document.getElementById("cog-search");
    search.addEventListener("input", (e) => { state.search = e.target.value; renderGrid(); });

    document.querySelectorAll("#filter-tags .tag-opt").forEach((t) => {
      t.addEventListener("click", () => { state.filter = t.dataset.f; render(view, topbarEl); });
    });
    document.getElementById("sort-select").addEventListener("change", (e) => { state.sort = e.target.value; renderGrid(); });
    document.getElementById("batch-toggle").addEventListener("change", (e) => { state.batch = e.target.checked; state.selected.clear(); render(view, topbarEl); });

    // 注入演示交互
    bindInjectDemo();

    renderGrid();
  }

  function renderGrid() {
    const grid = document.getElementById("cog-grid");
    if (!grid) return;
    if (state.loading) {
      grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="emoji">⏳</div><div>正在加载认知库…</div></div>`;
      return;
    }
    if (state.error) {
      grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="emoji">!</div><div>${escapeHTML(state.error)}</div><button class="btn btn-primary btn-sm" id="retry-cog">重试</button></div>`;
      const retry = document.getElementById("retry-cog");
      if (retry) retry.addEventListener("click", () => { state.loaded = false; loadItems().then(refresh); });
      return;
    }
    let list = getList();
    if (state.filter !== "全部") list = list.filter((c) => c.type === state.filter);
    if (state.search) { const q = state.search.toLowerCase(); list = list.filter((c) => (c.title + c.content + (c.tags || []).join(" ")).toLowerCase().includes(q)); }
    if (state.sort === "最新") list = [...list].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
    if (state.sort === "标签") list = [...list].sort((a, b) => b.tags.length - a.tags.length);

    if (!list.length) { grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="emoji">🗂️</div><div>没有匹配的认知条目</div></div>`; return; }

    grid.innerHTML = list.map((c) => cardHTML(c)).join("");
    grid.querySelectorAll(".cog-card").forEach((card) => {
      const id = card.dataset.id;
      const item = getList().find((c) => c.id === id);
      card.addEventListener("mousemove", (e) => {
        const r = card.getBoundingClientRect();
        card.style.setProperty("--mx", (e.clientX - r.left) + "px");
        card.style.setProperty("--my", (e.clientY - r.top) + "px");
      });
      card.querySelector(".inject-toggle input").addEventListener("click", (e) => e.stopPropagation());
      card.querySelector(".inject-toggle input").addEventListener("change", async (e) => {
        const injected = e.target.checked;
        e.target.disabled = true;
        try {
          const updated = await Yiyu.api.updateMemory(id, { status: injected ? "active" : "archived" });
          replaceItem(updated.item);
          Yiyu.modal.toast(injected ? "已启用注入" : "已关闭注入");
          renderGrid();
        } catch (err) {
          e.target.checked = !injected;
          Yiyu.modal.toast("保存失败：" + err.message, "danger");
        } finally {
          e.target.disabled = false;
        }
      });
      card.querySelector(".more-btn").addEventListener("click", (e) => { e.stopPropagation(); openMenu(card, item); });
      if (state.batch) {
        card.addEventListener("click", () => toggleSelect(id, card));
        const cb = card.querySelector(".batch-check");
        cb.addEventListener("click", (e) => { e.stopPropagation(); toggleSelect(id, card); });
      } else {
        card.addEventListener("click", () => openEditor(item));
      }
    });
  }

  function cardHTML(c) {
    const colorCls = TYPE_COLOR[c.type] || "chip";
    const sel = state.selected.has(c.id) ? "selected" : "";
    const checks = state.batch ? `<div class="batch-check" style="position:absolute;top:14px;right:14px"><span class="switch"><input type="checkbox" ${sel ? "checked" : ""}><span class="slider"></span></span></div>` : "";
    return `
      <div class="cog-card card ${sel}" data-id="${c.id}" style="position:relative">
        ${checks}
        <div class="card-top">
          <div style="display:flex;gap:6px;align-items:center">
            <span class="chip ${colorCls}">${c.type}</span>
            <span class="src-badge ${c.source === "ai" ? "src-ai" : "src-manual"}">${c.source === "ai" ? "AI 蒸馏" : "手动"}</span>
          </div>
          <button class="more-btn">${MORE}</button>
        </div>
        <div class="card-title">${escapeHTML(c.title)}</div>
        <div class="card-body">${escapeHTML(c.content)}</div>
        <div class="card-foot">
          <div style="display:flex;flex-direction:column;gap:8px">
            <div class="tags">${(c.tags || []).map((t) => `<span class="chip">${escapeHTML(t)}</span>`).join("")}</div>
            <div class="meta">更新于 ${c.updatedAt}</div>
          </div>
          <label class="inject-row" title="是否在研究中自动注入">
            <span class="switch inject-toggle"><input type="checkbox" ${c.injected ? "checked" : ""}><span class="slider"></span></span>
            注入
          </label>
        </div>
      </div>`;
  }

  function openMenu(card, item) {
    document.querySelectorAll(".dropdown-menu").forEach((m) => m.remove());
    const menu = document.createElement("div");
    menu.className = "dropdown-menu";
    menu.innerHTML = `
      <button data-act="edit">${EDIT} 编辑</button>
      <button data-act="dup">${PLUS} 复制为新条目</button>
      <button class="danger" data-act="del">${TRASH} 删除</button>`;
    card.appendChild(menu);
    menu.addEventListener("click", (e) => {
      const act = e.target.closest("button").dataset.act;
      menu.remove();
      if (act === "edit") openEditor(item);
      if (act === "del") confirmDelete(item);
      if (act === "dup") duplicateItem(item);
    });
    setTimeout(() => document.addEventListener("click", function close(e) { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener("click", close); } }), 0);
  }

  function confirmDelete(item) {
    Yiyu.modal.open({
      title: "删除认知条目", bodyHTML: `<p class="t-secondary">确定要删除「${escapeHTML(item.title)}」吗？此操作不可撤销。</p>`,
      footerHTML: `<button class="btn btn-ghost" id="cn">取消</button><button class="btn btn-accent" id="cd" style="background:linear-gradient(135deg,var(--color-danger),#F87171)">删除</button>`,
      onMount(root) {
        root.querySelector("#cn").onclick = Yiyu.modal.close;
        root.querySelector("#cd").onclick = async () => {
          try {
            await Yiyu.api.deleteMemory(item.id);
            state.items = state.items.filter((c) => c.id !== item.id);
            Yiyu.modal.close();
            Yiyu.modal.toast("已删除", "danger");
            refresh();
          } catch (e) {
            Yiyu.modal.toast("删除失败：" + e.message, "danger");
          }
        };
      }
    });
  }

  /* ----- 新建 / 编辑 ----- */
  function openEditor(item) {
    const isEdit = !!item;
    const draft = item ? Object.assign({}, item) : { title: "", content: "", type: "选股标准", tags: [], injected: true, source: "manual" };
    if (!draft.tags) draft.tags = [];
    Yiyu.modal.open({
      title: isEdit ? "编辑认知" : "新建认知",
      bodyHTML: `
        <div class="field"><label>标题 *</label><input class="input" id="f-title" value="${escapeHTML(draft.title)}" placeholder="一句话概括这条认知"/></div>
        <div class="field"><label>内容</label><textarea class="input textarea" id="f-content" placeholder="详细描述这条认知的判断与理由…">${escapeHTML(draft.content)}</textarea></div>
        <div class="field"><label>类型</label><div class="tag-group" id="f-types">${TYPES.map((t) => `<span class="tag-opt ${draft.type === t ? "active" : ""}" data-t="${t}">${t}</span>`).join("")}</div></div>
        <div class="field"><label>关联标签</label><input class="input" id="f-tags" value="${draft.tags.join(" ")}" placeholder="用空格分隔，如：#贵州茅台 #消费"/></div>
        <label class="batch-toggle" style="gap:10px"><span class="switch"><input type="checkbox" id="f-inject" ${draft.injected ? "checked" : ""}><span class="slider"></span></span>作为研究上下文自动注入</label>`,
      footerHTML: `<button class="btn btn-ghost" id="fc">取消</button><button class="btn btn-primary" id="fs">${isEdit ? "保存" : "创建"}</button>`,
      onMount(root) {
        root.querySelector("#fc").onclick = Yiyu.modal.close;
        root.querySelectorAll("#f-types .tag-opt").forEach((t) => t.addEventListener("click", () => {
          root.querySelectorAll("#f-types .tag-opt").forEach((x) => x.classList.remove("active"));
          t.classList.add("active"); draft.type = t.dataset.t;
        }));
        root.querySelector("#fs").onclick = async () => {
          const title = root.querySelector("#f-title").value.trim();
          if (!title) { Yiyu.modal.toast("请填写标题", "danger"); return; }
          const content = root.querySelector("#f-content").value.trim();
          const tags = root.querySelector("#f-tags").value.trim().split(/\s+/).filter(Boolean).map((t) => t.startsWith("#") ? t : "#" + t);
          const injected = root.querySelector("#f-inject").checked;
          const saveBtn = root.querySelector("#fs");
          saveBtn.disabled = true;
          try {
            if (isEdit) {
              const updated = await Yiyu.api.updateMemory(item.id, toApiFields({ title, content, type: draft.type, tags, injected }));
              replaceItem(updated.item);
              Yiyu.modal.toast("已保存", "success");
            } else {
              const resp = await Yiyu.api.confirmMemory([{
                card_id: "manual:" + Date.now(),
                action: "confirm",
                candidate: toCandidate({ title, content, type: draft.type, tags, injected }),
              }], "manual");
              const saved = (resp.results || []).find((r) => r.saved);
              if (!saved) throw new Error((resp.results && resp.results[0] && resp.results[0].error) || "创建失败");
              if (!injected) await Yiyu.api.updateMemory(saved.id, { status: "archived" });
              state.loaded = false;
              await loadItems();
              Yiyu.modal.toast("已创建", "success");
            }
            Yiyu.modal.close(); refresh();
          } catch (e) {
            Yiyu.modal.toast("保存失败：" + e.message, "danger");
          } finally {
            saveBtn.disabled = false;
          }
        };
      }
    });
  }

  /* ----- 上下文注入演示 ----- */
  function injectDemoHTML() {
    const enabled = getList().filter((c) => c.injected);
    return `
      <div class="inject-demo card">
        <div class="demo-head" id="demo-head">
          <b data-edit-id="demo-head-title" data-edit-label="注入演示标题">认知如何参与研究？</b>
          <span class="sub">· 当你提问时，已启用注入的认知会被自动匹配并送进大模型上下文</span>
          <span style="margin-left:auto;color:var(--color-primary-soft)">▾</span>
        </div>
        <div class="demo-body">
          <div class="demo-col">
            <div class="col-title">已启用注入的认知（${enabled.length}）</div>
            <div class="demo-inject-list">
              ${enabled.map((c) => `<div class="demo-inject-item"><span class="chip ${TYPE_COLOR[c.type] || "chip"}" style="padding:1px 7px">${c.type}</span>${escapeHTML(c.title)}</div>`).join("")}
            </div>
          </div>
          <div class="demo-arrow">${ARROW}<span class="fs-xs t-muted">注入</span></div>
          <div class="demo-col demo-prompt">
            <div class="col-title">模拟一次研究提问</div>
            <input class="input" id="demo-q" placeholder="例如：贵州茅台的护城河稳固吗？" value="贵州茅台的护城河稳固吗？"/>
            <button class="btn btn-primary btn-sm" id="demo-run" style="align-self:flex-start">运行模拟</button>
            <div class="demo-highlight" id="demo-out">
              <div class="fs-sm t-secondary">点击下方「运行模拟」，查看哪些认知被注入到本次研究的上下文中。</div>
            </div>
          </div>
        </div>
      </div>`;
  }

  function bindInjectDemo() {
    const head = document.getElementById("demo-head");
    if (head) head.addEventListener("click", () => document.querySelector(".inject-demo").classList.toggle("collapsed"));
    const run = document.getElementById("demo-run");
    if (run) run.addEventListener("click", () => {
      const q = document.getElementById("demo-q").value.trim();
      const out = document.getElementById("demo-out");
      if (!q) { Yiyu.modal.toast("请输入问题", "danger"); return; }
      const enabled = getList().filter((c) => c.injected);
      const hits = enabled.filter((c) => {
        const hay = (c.title + " " + c.content + " " + (c.tags || []).join(" ")).toLowerCase();
        const toks = q.toLowerCase().split(/[\s，,。.？?、]+/).filter((t) => t.length >= 2);
        return toks.some((t) => hay.includes(t));
      });
      if (!hits.length) {
        out.innerHTML = `<div class="fs-sm t-secondary">没有匹配到认知条目。可尝试在问题中提及具体公司或主题（如「茅台」「白酒」「仓位」）。</div>`;
        return;
      }
      out.innerHTML = `<div class="fs-sm fw-600 t-strong">已向本次研究上下文注入 ${hits.length} 条认知：</div>` +
        hits.map((c) => `<div class="demo-hit"><b>[${c.type}]</b> ${escapeHTML(c.title)}</div>`).join("");
    });
  }

  function toggleSelect(id, card) {
    if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
    render(document.getElementById("route-view"), document.getElementById("topbar"));
    showBatchBar();
  }
  function showBatchBar() {
    document.querySelectorAll(".batch-bar").forEach((b) => b.remove());
    if (!state.batch || !state.selected.size) return;
    const bar = document.createElement("div");
    bar.className = "batch-bar";
    bar.innerHTML = `<span class="count">已选 ${state.selected.size} 项</span><button class="btn btn-ghost btn-sm" id="b-export">导出</button><button class="btn btn-accent btn-sm" id="b-del" style="background:linear-gradient(135deg,var(--color-danger),#F87171)">删除</button>`;
    document.body.appendChild(bar);
    bar.querySelector("#b-del").onclick = async () => {
      const ids = Array.from(state.selected);
      try {
        await Promise.all(ids.map((id) => Yiyu.api.deleteMemory(id)));
        state.items = state.items.filter((c) => !state.selected.has(c.id));
        state.selected.clear(); Yiyu.modal.toast("已批量删除", "danger"); refresh();
      } catch (e) {
        Yiyu.modal.toast("批量删除失败：" + e.message, "danger");
      }
    };
    bar.querySelector("#b-export").onclick = () => Yiyu.modal.toast("已导出 JSON（演示）", "success");
  }

  async function duplicateItem(item) {
    try {
      const resp = await Yiyu.api.confirmMemory([{
        card_id: "copy:" + Date.now(),
        action: "confirm",
        candidate: toCandidate(Object.assign({}, item, { title: item.title + "（副本）" })),
      }], "manual");
      const saved = (resp.results || []).find((r) => r.saved);
      if (!saved) throw new Error((resp.results && resp.results[0] && resp.results[0].error) || "复制失败");
      if (!item.injected) await Yiyu.api.updateMemory(saved.id, { status: "archived" });
      state.loaded = false;
      await loadItems();
      Yiyu.modal.toast("已复制", "success");
      refresh();
    } catch (e) {
      Yiyu.modal.toast("复制失败：" + e.message, "danger");
    }
  }

  function replaceItem(raw) {
    const item = normalizeItem(raw);
    const idx = state.items.findIndex((c) => c.id === item.id);
    if (idx >= 0) state.items[idx] = item;
    else state.items.unshift(item);
  }

  function normalizeItem(raw) {
    const category = raw.category || "自定义";
    const tags = [];
    if (raw.symbol) tags.push("#" + raw.symbol);
    if (raw.scope) tags.push("#" + raw.scope);
    if (raw.subject_scope === "company") tags.push("#公司认知");
    return {
      raw,
      id: raw.id,
      title: raw.statement || "",
      content: raw.content || raw.basis || "",
      type: TYPES.includes(category) ? category : category || "自定义",
      tags,
      injected: raw.status === "active" || raw.status === "confirmed",
      source: raw.owner === "agent" || raw.source === "research_extract" ? "ai" : "manual",
      createdAt: dateOnly(raw.first_seen_at),
      updatedAt: dateOnly(raw.updated_at),
    };
  }

  function toApiFields(item) {
    return {
      statement: item.title,
      content: item.content,
      category: item.type,
      status: item.injected ? "active" : "archived",
      scope: tagsToScope(item.tags),
    };
  }

  function toCandidate(item) {
    return Object.assign({}, item.raw || {}, {
      statement: item.title,
      content: item.content,
      category: item.type,
      type: "cognition",
      subject_scope: "general",
      status: item.injected ? "active" : "archived",
      scope: tagsToScope(item.tags),
    });
  }

  function tagsToScope(tags) {
    return (tags || []).map((t) => String(t).replace(/^#/, "")).join(" ");
  }

  function today() { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
  function dateOnly(value) { return value ? String(value).slice(0, 10) : today(); }
  function escapeHTML(s) { return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  Yiyu.cognition = { render };
})();
