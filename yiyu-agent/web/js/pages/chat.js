/* ============================================================
   公司研究对话页 (window.Yiyu.chat)
   改造：欢迎页增加引导提问(参照问财风格)
         输入框缩小更紧凑
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});

  const SEND = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>';
  const STAR = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l3 6.5 7 .9-5 4.8 1.3 7L12 18.5 5.4 21.2 6.7 14.2 1.7 9.4l7-.9z"/></svg>';

  let scrollEl, composerEl;
  let activeResult = null;

  /* ----- 工具 ----- */
  function $(html) { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstElementChild; }
  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
  function scrollBottom() { scrollEl.scrollTop = scrollEl.scrollHeight; }

  /* 从历史记录进入时携带的 session_id（用于续跑/恢复，后端 load 检查点） */
  let pendingSessionId = null;

  function topbar(topbarEl) {
    topbarEl.innerHTML = `
      <div class="page-title">
        <b data-edit-id="chat-title" data-edit-label="对话页标题">以渔</b><span class="sub" data-edit-id="chat-sub" data-edit-label="对话页副标题">投研认知陪练</span>
      </div>
      <div class="topbar-right"></div>`;
  }

  /* ---- 欢迎页：猫头像 + 试试这样问（小猫/小鱼图标） ---- */
  function welcome() {
    return `
      <div class="welcome">
        <div class="welcome-header">
          <img class="logo-big" src="assets/logo-cat.png" alt="以渔" data-edit-id="welcome-logo" data-edit-type="icon" data-edit-label="欢迎页 Logo" style="border-radius:20px;object-fit:cover;" onerror="this.style.display='none'"/>
          <div class="welcome-text">
            <h1 data-edit-id="welcome-title" data-edit-label="欢迎标题">你好，我是以渔</h1>
            <p data-edit-id="welcome-desc" data-edit-label="欢迎简介">告诉我你想研究的公司或问题，我会带着你的认知库一起给出有证据、可追问的研究结论。</p>
          </div>
        </div>

        <!-- 试试这样问（分类快捷入口，小猫/小鱼图标） -->
        <div class="guide-section guide-qa">
          <div class="guide-head">
            <span class="guide-icon">✨</span>
            <b data-edit-id="qa-section-title" data-edit-label="引导区块标题">试试这样问</b>
            <span class="guide-sub">点击即可开始对话</span>
          </div>
          <div class="qa-grid">
            <div class="qa-col">
              <div class="qa-tag"><img class="qt-img" src="assets/icon-cat.png" alt="" data-edit-id="qa-ic-fund" data-edit-type="icon" data-edit-label="基本面研究图标" onerror="this.style.display='none'"/> <span data-edit-id="qa-label-fund" data-edit-label="基本面研究标签">基本面研究</span></div>
              <div class="qa-item" data-q="贵州茅台" data-edit-id="qa-1" data-edit-label="引导问句1">贵州茅台的护城河</div>
              <div class="qa-item" data-q="宁德时代" data-edit-id="qa-2" data-edit-label="引导问句2">宁德时代还能撑多久</div>
              <div class="qa-item" data-q="招商银行" data-edit-id="qa-3" data-edit-label="引导问句3">招商银行的资产质量</div>
            </div>
            <div class="qa-col">
              <div class="qa-tag"><img class="qt-img" src="assets/icon-fish.png" alt="" data-edit-id="qa-ic-quick" data-edit-type="icon" data-edit-label="快速判断图标" onerror="this.style.display='none'"/> <span data-edit-id="qa-label-quick" data-edit-label="快速判断标签">快速判断</span></div>
              <div class="qa-item" data-q="片仔癀估值贵不贵" data-edit-id="qa-4" data-edit-label="引导问句4">片仔癀估值贵不贵</div>
              <div class="qa-item" data-q="今日大盘走势" data-edit-id="qa-5" data-edit-label="引导问句5">今日大盘走势</div>
              <div class="qa-item" data-q="今日热门板块" data-edit-id="qa-6" data-edit-label="引导问句6">今日热门板块有哪些</div>
            </div>
            <div class="qa-col">
              <div class="qa-tag"><img class="qt-img" src="assets/icon-detect.png" alt="" data-edit-id="qa-ic-deep" data-edit-type="icon" data-edit-label="深度分析图标" onerror="this.style.display='none'"/> <span data-edit-id="qa-label-deep" data-edit-label="深度分析标签">深度分析</span></div>
              <div class="qa-item" data-q="英伟达商业模式拆解" data-edit-id="qa-7" data-edit-label="引导问句7">英伟达商业模式拆解</div>
              <div class="qa-item" data-q="近三天资金流入最多的板块" data-edit-id="qa-8" data-edit-label="引导问句8">近三天资金流入最多的板块</div>
              <div class="qa-item" data-q="目标价大于30%的个股" data-edit-id="qa-9" data-edit-label="引导问句9">目标价大于30%的个股</div>
            </div>
          </div>
        </div>
      </div>`;
  }

  function composerHTML() {
    return `
      <div class="composer-inner">
        <textarea id="chat-input" rows="1" placeholder="输入公司名称、代码或问题..."></textarea>
        <button class="send-btn" id="send-btn" title="发送">${SEND}</button>
      </div>`;
  }

  /* ----- 渲染用户 / AI 消息 ----- */
  function addUserMsg(text) {
    const m = $(`<div class="msg user"><div class="bubble">${escapeHTML(text)}</div><div class="avatar">${Yiyu.data.user.name.charAt(0)}</div></div>`);
    scrollEl.appendChild(m); scrollBottom();
  }

  function addAIMsg() {
    const m = $(`<div class="msg ai"><img class="avatar" src="assets/logo-cat.png" alt="" style="border-radius:50%;object-fit:cover;" onerror="this.style.display='none'"/><div class="bubble" id="ai-bubble"></div></div>`);
    scrollEl.appendChild(m); scrollBottom();
    return m.querySelector("#ai-bubble");
  }

  // 半截中间产物的特征：模型在组织答案时超时，流式里会留下 JSON / Evidence Pack 残片。
  // 这类内容不是结论，绝不能直接渲染成最终答案（历史 bug：半截 Evidence Pack + 研究已达时间上限）。
  function looksLikeIntermediate(text) {
    const t = (text || "").trim();
    if (!t) return false;
    return (
      t.includes("```json") ||
      t.includes("Evidence Pack") ||
      (t.startsWith("{") && t.length < 200) ||
      (t.startsWith("[") && t.length < 200)
    );
  }

  // 服务端没给正文、流式内容又不可读时的兜底文案
  const DEGRADED_FALLBACK_TEXT =
    "本次研究未能在时间上限内组织出完整结论，已停止，不输出买卖结论。\n\n" +
    "建议稍后重试，或把问题缩小为一个维度（例如只看估值或增长）。\n\n" +
    "AI 置信度很低，不代表投资确定性。";

  /* ----- 三阶段分析进度（对外契约 stage 枚举 → 固定文案） -----
     内部路由 / 工具名 / 模型思考一律不进 UI，只按阶段推进进度 */
  const PROGRESS_STAGES = [
    { title: "理解研究问题", sub: "正在明确标的与分析重点" },
    { title: "收集并核验信息", sub: "正在核对关键数据和公开资料" },
    { title: "整理分析结论", sub: "正在综合证据并检查风险" },
  ];
  const STAGE_INDEX = { understand_question: 0, evidence_check: 1, synthesizing: 2 };

  /* ----- 真实研究：消费后端 SSE 流 -----
     事件 → UI 映射（用户可理解的三阶段进度，不展示内部细节）：
       accepted/routing/preloop_progress → 阶段1 进行中
       preloop/plan → 阶段1 完成 + 本次分析重点面板
       progress(新契约) → 按 metadata.stage 推进对应阶段
       thought/tool_call/tool_result → 兜底映射为阶段2（不展示原文/工具名）
       answer_delta → 阶段3 + 流式正文
       final_answer → 全部完成，进度收起为「✓ 分析完成」
       ask_confirmation → 认知确认卡（确认/编辑/拒绝 → /memory/confirm） */
  async function runResearch(query) {
    const input = document.getElementById("chat-input");
    if (input) input.value = "";
    addUserMsg(query);
    const bubble = addAIMsg();
    bubble.innerHTML = `<span class="typing-dots"><i></i><i></i><i></i></span>`;
    scrollBottom();

    // 未登录先弹登录；取消则放弃本次发送
    if (!Yiyu.api.isAuthed()) {
      const ok = await Yiyu.api.ensureLogin();
      if (!ok) { bubble.remove(); return; }
    }

    // 发送中禁用输入，避免并发会话
    const sendBtn = document.getElementById("send-btn");
    if (input) input.disabled = true;
    if (sendBtn) sendBtn.disabled = true;

    const t0 = Date.now();
    let thinking = null, body = null;
    let textEl = null, answerText = "";
    let finalMeta = null;

    function ensureThinking() {
      if (thinking) return;
      thinking = $(`
        <div class="thinking" id="thinking">
          <div class="thinking-head"><span class="chev">${'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M6 9l6 6 6-6"/></svg>'}</span> <span class="thinking-title">分析进度</span></div>
          <div class="thinking-body">
            ${PROGRESS_STAGES.map((s, i) => `
            <div class="stage" data-stage="${i}" data-status="pending">
              <span class="stage-dot"></span>
              <div class="stage-main"><b>${s.title}</b><span class="stage-sub">${s.sub}</span></div>
            </div>`).join("")}
          </div>
        </div>`);
      thinking.querySelector(".thinking-head").addEventListener("click", () => thinking.classList.toggle("collapsed"));
      bubble.innerHTML = ""; bubble.appendChild(thinking);
      body = thinking.querySelector(".thinking-body");
    }
    /* 三阶段进度：setStage 只在原位置更新状态，绝不追加技术步骤 */
    function markStage(el, status) {
      el.dataset.status = status;
      const dot = el.querySelector(".stage-dot");
      dot.innerHTML = status === "done" ? "✓" : "";
    }
    function setStage(idx, status, subText) {
      ensureThinking();
      if (idx == null || idx < 0 || idx > PROGRESS_STAGES.length - 1) return;
      // 阶段只能前进：进入 N 阶段时，前面未完成的阶段自动标记完成
      for (let i = 0; i < idx; i++) {
        const prev = body.querySelector(`.stage[data-stage="${i}"]`);
        if (prev && prev.dataset.status !== "done") markStage(prev, "done");
      }
      const el = body.querySelector(`.stage[data-stage="${idx}"]`);
      if (!el) return;
      markStage(el, status);
      if (subText) el.querySelector(".stage-sub").textContent = subText;
      scrollBottom();
    }
    /* 全部完成：收起为「✓ 分析完成 · 查看过程」 */
    function finishAllStages() {
      ensureThinking();
      body.querySelectorAll(".stage").forEach((el) => markStage(el, "done"));
      const title = thinking.querySelector(".thinking-title");
      if (title) title.textContent = "✓ 分析完成 · 查看过程";
      thinking.classList.add("collapsed");
    }
    /* 进度区内的安全提示行（同一时刻最多一条，重复更新不追加） */
    function addNotice(text) {
      ensureThinking();
      let notice = body.querySelector(".stage-notice");
      if (!notice) {
        notice = document.createElement("div");
        notice.className = "stage-notice";
        body.appendChild(notice);
      }
      notice.textContent = text;
      scrollBottom();
    }
    /* 分析重点面板：展示「研究框架主题」，不是内部任务清单。
       优先读 metadata.focus_items（后端归纳后的用户视角主题）；
       兜底读 p0_questions / content JSON，但一律不显示「待分析」等任务状态徽章
       ——那是内部任务管理器视角，用户需要的是"按什么框架研究"。 */
    function renderPlanPanel(content, meta) {
      ensureThinking();
      let focus = [];
      let items = [];
      const metaPlan = (meta && meta.plan) || {};

      // 1) 研究框架主题（主渲染路径）
      if (Array.isArray(meta.focus_items) && meta.focus_items.length) {
        focus = meta.focus_items;
      } else if (Array.isArray(metaPlan.focus_items) && metaPlan.focus_items.length) {
        focus = metaPlan.focus_items;
      } else {
        try {
          const plan = JSON.parse(content || "{}");
          if (Array.isArray(plan.focus_items) && plan.focus_items.length) focus = plan.focus_items;
        } catch (e) { /* 解析失败不阻断主流程 */ }
      }

      // 2) 兜底：问题原文（不展示状态徽章）
      if (Array.isArray(meta.p0_questions) && meta.p0_questions.length) {
        items = meta.p0_questions;
      } else if (Array.isArray(metaPlan.p0_questions) && metaPlan.p0_questions.length) {
        items = metaPlan.p0_questions;
      } else {
        try {
          const plan = JSON.parse(content || "{}");
          items = plan.p0_questions || [];
        } catch (e) { /* 解析失败不阻断主流程 */ }
      }

      if (!focus.length && !items.length) return;
      let panel = body.querySelector(".plan-panel");
      if (panel) panel.remove(); // 计划更新时原地重绘，不追加

      const focusHTML = focus.length
        ? `<div class="focus-list">
             ${focus.map((f) => `
               <div class="focus-item">
                 <span class="focus-title">${escapeHTML(f.title || "")}</span>
                 <span class="focus-summary">${escapeHTML(f.summary || "")}</span>
               </div>`).join("")}
           </div>`
        : "";
      // 兜底列表：去掉 plan-status 徽章，纯问题文本
      const listHTML = (!focus.length && items.length)
        ? `<ol class="plan-list">
             ${items.map((p) => `<li class="plan-q">${escapeHTML(p.question || p.q || "")}</li>`).join("")}
           </ol>`
        : "";

      panel = $(`
        <div class="plan-panel">
          <div class="plan-head">本次分析重点</div>
          ${focusHTML}${listHTML}
        </div>`);
      body.appendChild(panel); scrollBottom();
    }
    function ensureText() {
      if (textEl) return;
      // 追加正文而非清空 bubble：进度组件保留在上方，完成后收起为「✓ 分析完成 · 查看过程」
      // 类名 .md-body 让 markdown.js 的渲染样式（标题/列表/表格/引用/分隔线/数字）生效；
      // .md-stream 仅作语义标记，将来要改流式样式可以单独覆盖。
      textEl = document.createElement("div");
      textEl.className = "md-body md-stream";
      textEl.style.cssText = "margin-top:8px;";
      bubble.appendChild(textEl);
    }

    const result = await Yiyu.api.streamChat({
      message: query,
      sessionId: pendingSessionId || ("web-" + Date.now().toString(36)),
      onEvent: (evt) => {
        const type = evt.type || "";
        const meta = evt.metadata || {};
        if (type === "accepted") {
          setStage(0, "running");
        } else if (type === "routing") {
          setStage(0, "running", "正在明确分析范围");
        } else if (type === "preloop_progress") {
          // 心跳：同一行更新，不新增步骤
          setStage(0, "running", meta.elapsed_sec != null ? `正在制定分析重点（已进行 ${meta.elapsed_sec} 秒）` : "正在制定分析重点");
        } else if (type === "preloop") {
          setStage(0, "done");
          setStage(1, "running");
          renderPlanPanel(evt.content, meta);
        } else if (type === "plan") {
          renderPlanPanel(evt.content, meta);
        } else if (type === "progress") {
          // 对外进度契约：metadata 只含 stage/status/sources 白名单
          const idx = STAGE_INDEX[meta.stage];
          if (idx != null) setStage(idx, meta.status === "done" ? "done" : "running");
        } else if (type === "thought") {
          // 兜底：不展示模型思考原文，只推进阶段
          setStage(1, "running");
        } else if (type === "tool_call") {
          // 兜底：不展示工具名与参数
          setStage(1, "running", "正在核验信息");
        } else if (type === "tool_result") {
          // 兜底：不展示工具名与结果摘要，来源统一由最终报告的引用清单承载
          setStage(1, "running");
        } else if (type === "answer_delta") {
          ensureText();
          setStage(2, "running");
          answerText += evt.content || "";
          // 流式阶段直接渲染 markdown——# / ** / 列表不再残留为原文。
          // markdown.js 是 escape-first 渲染，模型的原始 HTML 不会进页面。
          try {
            textEl.innerHTML = Yiyu.md.render(answerText, {}).html;
          } catch (e) {
            // 极端情况（半截 markdown 触发解析抛错）降级为纯文本，绝不让页面卡死
            textEl.textContent = answerText;
          }
          scrollBottom();
        } else if (type === "warning") {
          // 不展示原始告警文本（可能含内部波动细节）
          addNotice("研究过程中出现波动，正在自动恢复");
        } else if (type === "error") {
          // 超时/错误时：如果流式内容像中间 JSON 或 Evidence Pack，不展示为最终答案
          const errMsg = evt.content || "研究过程出现异常，已停止。可稍后重试，或把问题缩小为一个维度。";
          if (looksLikeIntermediate(answerText)) {
            // 中间 JSON/Evidence Pack 不应作为最终结论展示，清空并只显示错误/降级信息
            answerText = "";
            textEl.textContent = "";
          }
          ensureText();
          textEl.insertAdjacentHTML("beforeend",
            `<div style="margin-top:8px;color:var(--color-danger,#c0392b)">⚠ ${escapeHTML(errMsg)}</div>`);
          scrollBottom();
        } else if (type === "final_answer") {
          // 以服务端全文为准：硬规则可能已修正/替换流式文本
          const serverContent = (evt.content || "").trim();
          if (serverContent) {
            answerText = serverContent;
          } else if (looksLikeIntermediate(answerText)) {
            // 服务端没给正文，而流式里是半截 JSON / Evidence Pack：
            // 不能拿中间产物充当最终结论，替换为用户可读的降级说明
            answerText = DEGRADED_FALLBACK_TEXT;
          }
          // 否则保留流式累积的 answerText
          finalMeta = meta;
          finishAllStages();
          // 定稿：升级为「研究报告」版式（安全 Markdown 渲染 + 来源抽屉 + 认知陪练卡）
          try {
            renderReport(bubble, answerText, meta.citations || [], query, meta);
          } catch (e) {
            // 渲染失败兜底：直接把 markdown 渲染结果放回，绝不展示半截原文
            console.error("[renderReport] failed", e);
            if (textEl && textEl.parentNode) textEl.parentNode.removeChild(textEl);
            textEl = null;
            const fb = document.createElement("div");
            fb.className = "md-body";
            try {
              fb.innerHTML = Yiyu.md.render(answerText || DEGRADED_FALLBACK_TEXT, {}).html;
            } catch (_) {
              fb.textContent = answerText || DEGRADED_FALLBACK_TEXT;
            }
            bubble.appendChild(fb);
          }
          scrollBottom();
        } else if (type === "ask_confirmation") {
          bubble.appendChild(buildConfirmCards(meta.cards || [], meta.source_task_id || ""));
          scrollBottom();
        }
        // complete / start / debug 等暂不渲染
      },
    });

    if (!result.ok && !textEl) {
      ensureText();
      textEl.textContent = "";
      textEl.insertAdjacentHTML("beforeend",
        `<div class="research-fail">
          <div class="fail-icon">⚠</div>
          <div class="fail-msg">${escapeHTML(result.error || "研究请求失败")}</div>
          <div class="fail-hint fs-xs t-muted">可重试，或缩小问题范围后再次发起。</div>
          <button class="btn btn-accent btn-sm" id="retry-btn">重新研究</button>
        </div>`);
      const retry = textEl.querySelector("#retry-btn");
      if (retry) retry.addEventListener("click", () => { bubble.remove(); runResearch(query); });
    }

    // 收尾信息条：耗时 / 降级 / 推断标注
    const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    const bits = [];
    bits.push(`耗时 ${elapsed}s`);
    if (finalMeta && finalMeta.degraded) {
      bits.push('<span style="color:var(--color-warning,#b8860b)">本次研究未完成完整取证（降级输出），仅供参考</span>');
    }
    if (finalMeta && finalMeta.auto_sanitized && (finalMeta.auto_sanitized_numbers || []).length) {
      bits.push(`其中 ${finalMeta.auto_sanitized_numbers.length} 个数字无法溯源，已标注为「推断」`);
    }
    if (finalMeta && finalMeta.validated === false) {
      bits.push('<span style="color:var(--color-danger,#c0392b)">结论未通过安全校验</span>');
    }
    const info = document.createElement("div");
    info.className = "fs-xs t-muted";
    info.style.marginTop = "10px";
    info.innerHTML = bits.join("　·　");
    bubble.appendChild(info);
    // 来源不再在正文底部堆列表：统一由报告右侧抽屉承载（final_answer.citations）
    scrollBottom();

    if (result.needLogin) {
      await Yiyu.api.ensureLogin();
      if (input) { input.value = query; input.focus(); }
      return;
    }

    // 新会话/追加消息都会改变历史列表（首次提问决定标题），刷新侧边栏
    if (Yiyu.app && Yiyu.app.refreshHistory) Yiyu.app.refreshHistory();

    if (input) input.disabled = false;
    if (sendBtn) sendBtn.disabled = false;
  }

  /* ----- 研究报告版式：正文 Markdown + 来源抽屉 -----
     正文经 Yiyu.md 渲染（转义优先，模型的原始 HTML 不会直接进页面）；
     来源取 final_answer.metadata.citations，每条引用标记都可点击定位。 */
  const TYPE_CLASS = {
    财报: "t-report",
    行情数据: "t-quote",
    新闻与研报: "t-news",
    指标计算: "t-calc",
    公司公告: "t-notice",
    公司画像: "t-notice",
    个人认知库: "t-news",
    公开资料: "t-calc",
  };
  const LEVEL_RANK = { S: 3, A: 2, B: 1 };

  function latestAsOf(citations) {
    let best = "";
    (citations || []).forEach((c) => {
      const d = String((c && c.as_of) || "").trim();
      if (d && d > best) best = d; // ISO 日期串可直接比较
    });
    return best;
  }

  /* 证据完整度取木桶短板：S > A > B */
  function overallLevel(citations) {
    let min = 99, label = "";
    (citations || []).forEach((c) => {
      const lv = String((c && c.level) || "B").trim().toUpperCase();
      const r = LEVEL_RANK[lv] || 1;
      if (r < min) { min = r; label = lv; }
    });
    return label;
  }

  /* 找出引用标记所在的那句话，用于来源卡上的「哪句结论用了它」 */
  function citationContext(text, evidenceId) {
    const m = /^e?(\d+)$/i.exec(String(evidenceId || "").trim());
    if (!m) return "";
    const re = new RegExp("\\[e?" + m[1] + "\\]", "i");
    const lines = String(text || "").split("\n");
    for (const raw of lines) {
      if (re.test(raw)) {
        return raw
          .replace(/^\s*#{1,6}\s+/, "")
          .replace(/^\s*[-*+]\s+/, "")
          .replace(/\[e?\d+\]/gi, "")
          .replace(/\*\*/g, "")
          .trim()
          .slice(0, 90);
      }
    }
    return "";
  }

  /* ----- 展示层兜底：内部术语不进界面 -----
     后端已做收口，这里再挡一层：工具名、计划分级属于内部实现，
     用户该看到的是「这是什么来源」，不是 `base_pack` / `market_get_bundle`。 */
  const INTERNAL_TERMS = [
    [/market[._ ]?get[._ ]?bundle/gi, "行情与财务数据"],
    [/calc[._]base[._]pack|base[._]pack/gi, "系统指标计算"],
    [/cognition[._]recall/gi, "个人认知库"],
    [/company[._]classify|entity[._]resolver/gi, "公司档案"],
    [/web[._]search/gi, "公开资料检索"],
    [/data[._]requirement/gi, "待补充数据"],
  ];
  const INTERNAL_TOKEN = "market[._ ]?get[._ ]?bundle|calc[._]base[._]pack|base[._]pack|cognition[._]recall|web[._]search|entity[._]resolver|company[._]classify";

  function scrubInternal(text) {
    let out = String(text || "");
    // 括号里标注内部来源时（「ROIC 30.0%（base_pack，优秀档）」）只摘掉内部名，保留可读部分
    out = out.replace(new RegExp("[（(]\\s*(?:" + INTERNAL_TOKEN + ")\\s*[，,、]?\\s*", "gi"), "（");
    out = out.replace(new RegExp("\\s*[，,、]\\s*(?:" + INTERNAL_TOKEN + ")\\s*(?=[）)])", "gi"), "");
    out = out.replace(/[（(]\s*[）)]/g, "");            // 清掉被掏空的括号
    out = out.replace(/\s*[（(]\s*P[012]\s*[）)]/gi, ""); // 计划分级是内部任务态
    INTERNAL_TERMS.forEach(([re, label]) => { out = out.replace(re, label); });
    return out;
  }

  /* ----- 报告标题：取正文首个 H1/H2，正文里不再重复渲染 -----
     标题里的证券代码单独提出来做成小标签（600519.SH）。 */
  const CODE_RE = /\b(\d{6}\.(?:SH|SZ|BJ|HK)|\d{4,5}\.HK)\b/i;
  const TITLE_TAIL_RE = /\s*(?:研究)?(?:简报|报告|研究笔记)\s*$/;

  /* 前面的块被抽走后，正文开头可能只剩空行和 ---，没有信息量 */
  function stripLeadingRules(text) {
    const lines = String(text || "").split("\n");
    while (lines.length && (!lines[0].trim() || /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(lines[0]))) lines.shift();
    return lines.join("\n");
  }

  function splitLeadingTitle(text) {
    const lines = String(text || "").split("\n");
    for (let i = 0; i < Math.min(lines.length, 4); i++) {
      const m = /^#{1,2}\s+(.+?)\s*$/.exec(lines[i].trim());
      if (!m) continue;
      let title = m[1].replace(/\*\*/g, "").trim();
      const codeM = CODE_RE.exec(title);
      const code = codeM ? codeM[1].toUpperCase() : "";
      if (code) title = title.replace(codeM[1], "").replace(/\s{2,}/g, " ").trim();
      title = title.replace(/[（(]\s*[）)]/g, "").replace(TITLE_TAIL_RE, "").trim();
      return { title, code, body: stripLeadingRules(lines.slice(i + 1).join("\n")) };
    }
    return { title: "", code: "", body: String(text || "") };
  }

  /* ----- 研究摘要卡 -----
     从正文抽「**键：值**」形式的结论句，卡片承接后正文不再重复。
     抽不满 2 条就不渲染：宁可不显示，也不把任意粗体句当成结论。 */
  const SUMMARY_HINTS = ["结论", "判断", "倾向", "质量", "护城河", "回报", "估值", "风险", "关注", "不确定", "现金流", "增长", "安全边际", "盈利"];

  function extractSummary(text) {
    const rows = [];
    const drop = new Set();
    const lines = String(text || "").split("\n");
    lines.forEach((raw, i) => {
      if (rows.length >= 5) return;
      const line = raw.trim().replace(/^[-*+]\s+/, "").replace(/^\d+[.)]\s+/, "");
      const m = /^\*\*(.{2,10}?)\s*[：:]\s*(.+?)\*\*\s*$/.exec(line) ||
        /^\*\*(.{2,10}?)\s*[：:]\*\*\s*(.+?)\s*$/.exec(line);
      if (!m) return;
      const k = m[1].trim();
      const v = m[2].trim();
      if (!v || v.length > 40) return;
      if (!SUMMARY_HINTS.some((h) => k.includes(h))) return;
      if (rows.some((r) => r.k === k)) { drop.add(i); return; }
      rows.push({ k, v });
      drop.add(i);
    });
    if (rows.length < 2) return { rows: [], body: String(text || "") };
    return { rows, body: lines.filter((_, i) => !drop.has(i)).join("\n") };
  }

  /* ----- 数据缺口：系统说明收成一行 + 「查看缺失数据」 -----
     用户要知道的是「缺什么」，不是「Web 搜索失败两次」这种内部状态。 */
  /* 只认真正的系统状态描述（「说明：本段为判断」这类正文引用不要吞） */
  const SYS_NOTE_RE = /本轮|地基指标|检索通道|外部信息|未返回|取数失败|降级说明|快照/;
  const GAP_HEAD_RE = /^#{2,4}\s+.*(?:数据缺口|缺失|待验证|仍需验证|未取得|证据不足|需一手验证)/;
  const GAP_ITEM_RE = /^([-*+]\s+|[①②③④⑤⑥⑦⑧⑨⑩]|\d+[.)]\s+|缺失[：:])/;

  function pushGap(gaps, item) {
    let s = String(item || "").trim()
      .replace(/^(?:说明|备注|缺失)\s*[：:]\s*/, "")
      .replace(/^[①②③④⑤⑥⑦⑧⑨⑩]\s*/, "")
      .trim();
    if (!s) return;
    if (s.length > 50) s = s.slice(0, 50) + "…";
    if (!gaps.includes(s)) gaps.push(s);
  }

  function extractDataGaps(text, meta) {
    const gaps = [];
    const drop = new Set();
    const lines = String(text || "").split("\n");
    let inGapSection = false;

    lines.forEach((raw, i) => {
      const line = raw.trim();
      if (/^#{1,6}\s+/.test(line)) {
        inGapSection = GAP_HEAD_RE.test(line);
        if (inGapSection) drop.add(i); // 章节整体收进组件，正文不重复列一遍
        return;
      }
      // 引用块里的系统说明：折叠进缺口组件，不占正文首屏
      if (/^>\s?/.test(line)) {
        const note = line.replace(/^>\s?/, "");
        if (SYS_NOTE_RE.test(note)) {
          drop.add(i);
          note.split(/[；;。]/).forEach((c) => pushGap(gaps, c));
        }
        return;
      }
      // 只收条目行，段落留在正文里，避免把可读内容整段搬走
      if (!inGapSection || !GAP_ITEM_RE.test(line)) return;
      drop.add(i);
      pushGap(gaps, line.replace(/^[-*+]\s+/, "").replace(/^\d+[.)]\s+/, ""));
    });

    if (meta && meta.degraded) pushGap(gaps, "本轮取证不完整，结论按降级输出");
    return { gaps: gaps.slice(0, 6), body: lines.filter((_, i) => !drop.has(i)).join("\n") };
  }

  function buildSummaryCard(rows) {
    if (!rows || !rows.length) return "";
    const body = rows.map((r) => `
      <div class="summary-row">
        <dt>${escapeHTML(r.k)}</dt>
        <dd>${Yiyu.md.render(r.v, {}).html}</dd>
      </div>`).join("");
    return `
      <div class="summary-card">
        <div class="summary-title">研究摘要</div>
        <dl class="summary-rows">${body}</dl>
      </div>`;
  }

  function buildDataGap(gaps) {
    if (!gaps || !gaps.length) return "";
    return `
      <details class="data-gap">
        <summary>
          <span class="gap-dot"></span>
          <span class="gap-text">本次研究存在数据缺口</span>
          <span class="gap-more">查看缺失数据</span>
        </summary>
        <ul class="gap-list">${gaps.map((g) => `<li>${escapeHTML(g)}</li>`).join("")}</ul>
      </details>`;
  }

  function buildReportHead(query, head, citations, meta) {
    const list = citations || [];
    const asOf = latestAsOf(list);
    const level = overallLevel(list);
    const bits = [];
    bits.push("<span>价值投资研究</span>");
    if (asOf) bits.push(`<span>数据截至 ${escapeHTML(asOf)}</span>`);
    bits.push(`<span>${list.length} 个来源</span>`);
    if (level) bits.push(`<span class="ev-badge ${escapeHTML(level)}">证据完整度 ${escapeHTML(level)} 级</span>`);
    if (meta && meta.degraded) bits.push('<span class="ev-badge degraded">取证不完整</span>');
    return `
      <div class="report-head">
        <div class="report-head-main">
          <h2 class="report-title">${escapeHTML(head.title || query)}${head.code ? `<span class="report-code">${escapeHTML(head.code)}</span>` : ""}</h2>
          <div class="report-meta">${bits.join('<span class="sep">·</span>')}</div>
        </div>
      </div>`;
  }

  /* 后端来源类型 → 用户能核对的三类说法：
     公开资料（年报/公告/新闻）/ 结构化行情与财务数据 / 系统计算指标。 */
  const SOURCE_KIND = {
    财报: "公开资料",
    公司公告: "公开资料",
    新闻与研报: "公开资料",
    公开资料: "公开资料",
    行情数据: "结构化行情与财务数据",
    指标计算: "系统计算指标",
    公司画像: "公司档案",
    个人认知库: "你的认知库",
  };
  function sourceKind(c) {
    return SOURCE_KIND[String((c && c.type) || "公开资料")] || "公开资料";
  }

  function buildSourcesPanel(citations, text) {
    const list = (citations || []).filter((c) => c && c.evidence_id);
    const drawerHead = `
        <div class="src-panel-grabber"></div>
        <button class="src-panel-close" type="button" aria-label="关闭来源抽屉">×</button>`;
    if (!list.length) {
      return `
      <div class="src-panel">${drawerHead}
        <div class="src-panel-title">来源与证据<span class="count">暂无</span></div>
        <div class="src-empty">本轮没有返回可核对的引用来源。<br/>正文中缺少来源标注的数字，请谨慎参考。</div>
      </div>`;
    }

    const cards = list.map((c, i) => {
      const evidenceId = String(c.evidence_id);
      const num = evidenceId.replace(/^e/i, "") || String(i + 1);
      const type = String(c.type || "公开资料");
      const rows = [];
      if (c.as_of) rows.push(["数据日期", escapeHTML(c.as_of)]);
      if (c.period) rows.push(["报告期", escapeHTML(c.period)]);
      const caliber = [c.metric_id, c.caliber_version].filter(Boolean).join(" · ");
      if (caliber) rows.push(["口径", escapeHTML(caliber)]);
      if (c.fields && c.fields.length) rows.push(["原始字段", escapeHTML(c.fields.join("、"))]);
      if (c.url) {
        const u = String(c.url);
        rows.push(["链接", /^https?:\/\//i.test(u)
          ? `<a href="${escapeHTML(u)}" target="_blank" rel="noopener noreferrer">${escapeHTML(u)}</a>`
          : escapeHTML(u)]);
      }
      const ctx = citationContext(text, evidenceId);
      return `
        <div class="src-card" data-ref="${escapeHTML(evidenceId)}">
          <div class="src-card-head">
            <span class="src-num">${escapeHTML(num)}</span>
            <span class="src-title">${escapeHTML(c.title || type)}</span>
          </div>
          <span class="src-kind">${escapeHTML(sourceKind(c))}</span>
          <span class="src-type ${TYPE_CLASS[type] || "t-calc"}">${escapeHTML(type)}</span>
          ${type === "指标计算" ? `<button type="button" class="src-caliber" data-ref="${escapeHTML(evidenceId)}">查看计算口径 ›</button>` : ""}
          ${rows.length ? `<div class="src-rows">${rows.map((r) => `<div class="src-row"><span class="k">${r[0]}</span><span class="v">${r[1]}</span></div>`).join("")}</div>` : ""}
          ${ctx ? `<div class="src-used">被引用：<b>${escapeHTML(ctx)}</b></div>` : ""}
        </div>`;
    }).join("");

    return `
      <div class="src-panel">${drawerHead}
        <div class="src-panel-title">来源与证据<span class="count">${list.length} 条</span></div>
        ${cards}
      </div>`;
  }

  /* 悬浮引用卡：hover [1] 直接看到「这是什么来源、什么数据、截止到哪天」 */
  function buildCiteTip(c) {
    const rows = [];
    const date = c.as_of || c.period;
    if (c.fields && c.fields.length) rows.push(["数据", c.fields.slice(0, 3).join("、")]);
    if (date) rows.push(["截止", date]);
    const caliber = [c.metric_id, c.caliber_version].filter(Boolean).join(" · ");
    if (caliber) rows.push(["口径", caliber]);
    return `
      <div class="cite-pop-title">${escapeHTML(c.title || sourceKind(c))}</div>
      <div class="cite-pop-kind">${escapeHTML(sourceKind(c))}${c.type ? ` · ${escapeHTML(c.type)}` : ""}</div>
      ${rows.length ? rows.map((r) => `<div class="cite-pop-row"><span>${r[0]}</span><b>${escapeHTML(r[1])}</b></div>`).join("") : ""}
      ${c.url ? `<div class="cite-pop-more">点击打开原始来源 ›</div>` : `<div class="cite-pop-more">点击查看更多 ›</div>`}`;
  }

  /* 引用标记 ↔ 来源卡：hover 出悬浮卡，点击打开抽屉并定位来源 */
  function bindCitations(root, citations) {
    const panel = root.querySelector(".src-panel");
    const mask = root.querySelector(".src-mask");
    const fab = root.querySelector(".src-fab");
    const closeBtn = root.querySelector(".src-panel-close");
    const pop = root.querySelector(".cite-pop");
    const cards = Array.from(root.querySelectorAll(".src-card"));
    const byId = {};
    (citations || []).forEach((c) => { if (c && c.evidence_id) byId[String(c.evidence_id).toLowerCase()] = c; });

    function openDrawer() {
      if (panel) panel.classList.add("open");
      if (mask) mask.classList.add("open");
    }
    function closeDrawer() {
      if (panel) panel.classList.remove("open");
      if (mask) mask.classList.remove("open");
    }
    function highlight(ref) {
      openDrawer();
      cards.forEach((c) => c.classList.toggle("active", c.dataset.ref === ref));
      const card = cards.find((c) => c.dataset.ref === ref);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      root.querySelectorAll(".cite-ref").forEach((b) => b.classList.toggle("active", b.dataset.ref === ref));
    }
    if (fab) fab.addEventListener("click", openDrawer);
    if (closeBtn) closeBtn.addEventListener("click", closeDrawer);
    if (mask) mask.addEventListener("click", closeDrawer);
    root.querySelectorAll(".src-caliber").forEach((btn) => {
      btn.addEventListener("click", () => highlight(btn.dataset.ref));
    });

    let hideTimer = null;
    function hidePop() {
      hideTimer = setTimeout(() => { if (pop) pop.hidden = true; }, 160);
    }
    function showPop(btn) {
      const c = byId[String(btn.dataset.ref || "").toLowerCase()];
      if (!c || !pop) return;
      clearTimeout(hideTimer);
      pop.innerHTML = buildCiteTip(c);
      pop.hidden = false;
      const r = btn.getBoundingClientRect();
      const host = root.getBoundingClientRect();
      const left = Math.min(Math.max(r.left - host.left + r.width / 2, 90), Math.max(host.width - 90, 90));
      pop.style.left = left + "px";
      pop.style.top = (r.top - host.top - 6) + "px";
    }

    root.querySelectorAll(".cite-ref").forEach((btn) => {
      btn.addEventListener("mouseenter", () => showPop(btn));
      btn.addEventListener("mouseleave", hidePop);
      btn.addEventListener("focus", () => showPop(btn));
      btn.addEventListener("blur", hidePop);
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const ref = btn.dataset.ref;
        if (!ref) return;
        // 有原始网页时直接打开来源；没有就展开来源抽屉说明这条证据是什么
        const c = byId[String(ref).toLowerCase()];
        if (c && /^https?:\/\//i.test(String(c.url || ""))) {
          window.open(c.url, "_blank", "noopener,noreferrer");
          return;
        }
        highlight(ref);
      });
    });

    // 鼠标移到悬浮卡上时不隐藏，方便点开来源
    if (pop) {
      pop.addEventListener("mouseenter", () => clearTimeout(hideTimer));
      pop.addEventListener("mouseleave", hidePop);
    }
  }

  function renderReport(bubble, text, citations, query, meta) {
    // 移除流式阶段的纯文本节点，换成排版后的报告
    if (textEl && textEl.parentNode) textEl.parentNode.removeChild(textEl);
    textEl = null;

    // 展示层流水线：洗掉内部术语 → 提标题 → 抽摘要 → 收数据缺口 → 渲染正文
    const head = splitLeadingTitle(scrubInternal(text));
    const summary = extractSummary(head.body);
    const gaps = extractDataGaps(summary.body, meta);
    const body = stripLeadingRules(gaps.body);
    const rendered = Yiyu.md.render(body, {});
    const list = (citations || []).filter((c) => c && c.evidence_id);
    const wrap = $(`
      <div class="report-block">
        ${buildReportHead(query, head, citations, meta)}
        ${buildSummaryCard(summary.rows)}
        ${buildDataGap(gaps.gaps)}
        <div class="report-layout">
          <div class="report-main">
            <div class="md-body">${rendered.html}</div>
          </div>
          <div class="report-sources">${buildSourcesPanel(citations, body)}</div>
        </div>
        ${list.length ? `<div class="report-sources-bar"><button class="src-fab" type="button">查看来源与证据（${list.length}）</button></div>` : ""}
        <div class="src-mask"></div>
        <div class="cite-pop" hidden></div>
      </div>`);

    // 让这条 AI 消息突破对话气泡的窄栏限制，报告可以用满宽度
    const msgEl = bubble.closest(".msg");
    if (msgEl) msgEl.classList.add("has-report");

    bubble.appendChild(wrap);
    bindCitations(wrap, list);

    // 认知陪练卡：报告完成后让用户写下自己的判断，再让 AI 追问。
    // 直接挂在 bubble 里，与报告平级，不嵌进 .report-block 以免受双栏布局影响。
    const coachCard = buildCoachCard(query, meta);
    if (coachCard) {
      bubble.appendChild(coachCard);
      bindCoachCard(coachCard, query);
    }
    return wrap;
  }

  /* ----- 认知陪练卡 -----
     报告完成后给用户一个「写下你的判断」的位置：先把观点写下来，再让 AI 追问。
     教练卡不主动调认知库 API（写库必须有 cognition candidate，由 ask_confirmation 事件承载）；
     这里只负责把用户判断作为新一轮提问送回 runResearch，沿用同一会话。 */
  function buildCoachCard(query, meta) {
    const card = `
      <div class="coach-card">
        <div class="coach-head">
          <span class="coach-tag">认知陪练</span>
          <b class="coach-title">写下你的判断</b>
        </div>
        <p class="coach-tip">AI 给的是参考，最终决策在你。先把你的判断写下来，再决定要不要采纳报告。</p>
        <textarea class="coach-input" rows="3" placeholder="比如：我觉得茅台的护城河来自品牌定价权，但当前 PE 已经透支了未来 3 年的提价..."></textarea>
        <div class="coach-actions">
          <button class="btn btn-accent btn-sm" id="coach-ask">让 AI 继续追问 →</button>
        </div>
      </div>`;
    return $(card);
  }

  function bindCoachCard(card, query) {
    const askBtn = card.querySelector("#coach-ask");
    const input = card.querySelector(".coach-input");
    if (!askBtn || !input) return;
    askBtn.addEventListener("click", () => {
      const userThought = (input.value || "").trim();
      if (!userThought) {
        input.focus();
        Yiyu.modal.toast("先写下你的判断，再让 AI 继续追问", "info");
        return;
      }
      input.disabled = true;
      askBtn.disabled = true;
      askBtn.textContent = "已发出，让 AI 追问…";
      // 把用户判断作为新一轮提问：runResearch 会沿用 pendingSessionId 追加到当前会话
      runResearch(userThought);
    });
  }

  /* ----- 认知确认卡（真实数据：ask_confirmation.metadata.cards) ----- */
  function buildConfirmCards(cards, sourceTaskId) {
    if (!cards.length) return document.createDocumentFragment();
    const wrap = document.createElement("div");
    wrap.className = "cog-deposit";
    wrap.style.marginTop = "12px";
    wrap.innerHTML = `<div class="head">${STAR} 认知沉淀 · 确认后才会写入你的认知库</div>`;
    cards.forEach((card) => {
      const candidate = card.candidate || card;
      const title = candidate.statement || candidate.title || "认知候选";
      const content = candidate.content || candidate.basis || "";
      const category = candidate.category || "认知";
      const el = $(`<div class="card" style="margin:8px 0;padding:var(--space-3)">
        <div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline">
          <b class="fs-sm">${escapeHTML(title)}</b>
          <span class="chip chip-accent">${escapeHTML(category)}</span>
        </div>
        <div class="fs-xs t-secondary" style="white-space:pre-wrap;margin-top:6px">${escapeHTML(content)}</div>
        <div class="actions" style="display:flex;gap:8px;margin-top:8px">
          <button class="btn btn-accent btn-sm">确认入库</button>
          <button class="btn btn-ghost btn-sm">我有不同看法</button>
          <button class="btn btn-ghost btn-sm">拒绝</button>
        </div>
      </div>`);
      const [okBtn, editBtn, noBtn] = el.querySelectorAll(".actions .btn");
      const setDone = (text) => {
        okBtn.disabled = true;
        editBtn.disabled = true;
        noBtn.disabled = true;
        okBtn.textContent = text;
      };
      okBtn.addEventListener("click", async () => {
        okBtn.disabled = true;
        try {
          await Yiyu.api.confirmMemory([{ card_id: card.card_id || "", action: "confirm", candidate }], sourceTaskId);
          Yiyu.modal.toast("已存入认知库", "success");
          setDone("已入库");
        } catch (e) {
          okBtn.disabled = false;
          Yiyu.modal.toast("保存失败：" + e.message, "error");
        }
      });
      editBtn.addEventListener("click", () => {
        Yiyu.modal.open({
          title: "编辑后确认",
          bodyHTML: `<div class="field"><label>我的表述</label>
            <textarea class="input textarea" id="cc-edit">${escapeHTML(candidate.statement || "")}</textarea></div>
            <div class="field"><label>补充说明</label>
            <textarea class="input textarea" id="cc-content">${escapeHTML(content)}</textarea></div>`,
          footerHTML: `<button class="btn btn-ghost" id="cc-cancel">取消</button>
                       <button class="btn btn-accent" id="cc-save">确认入库</button>`,
          onMount(root) {
            root.querySelector("#cc-cancel").onclick = Yiyu.modal.close;
            root.querySelector("#cc-save").onclick = async () => {
              const edited = root.querySelector("#cc-edit").value.trim();
              const editedContent = root.querySelector("#cc-content").value.trim();
              try {
                await Yiyu.api.confirmMemory(
                  [{
                    card_id: card.card_id || "",
                    action: "edit",
                    candidate,
                    edited: {
                      statement: edited || candidate.statement,
                      content: editedContent || candidate.content || "",
                    },
                  }],
                  sourceTaskId);
                Yiyu.modal.close();
                Yiyu.modal.toast("已按你的表述入库", "success");
                setDone("已入库");
              } catch (e) {
                Yiyu.modal.toast("保存失败：" + e.message, "error");
              }
            };
          },
        });
      });
      noBtn.addEventListener("click", async () => {
        noBtn.disabled = true;
        try {
          await Yiyu.api.confirmMemory([{ card_id: card.card_id || "", action: "reject", candidate }], sourceTaskId);
          el.remove();
        } catch (e) {
          noBtn.disabled = false;
          Yiyu.modal.toast("拒绝失败：" + e.message, "error");
        }
      });
      wrap.appendChild(el);
    });
    return wrap;
  }

  async function typeWriter(el, text, speed) {
    const tokens = text.split("");
    for (let i = 0; i < tokens.length; i++) {
      el.textContent += tokens[i];
      if (i % 2 === 0) { scrollBottom(); await sleep(speed); }
    }
  }

  /* ----- 结构化结果面板 ----- */
  function buildResultPanel(sample) {
    const ov = sample.overview;
    const tabs = ["overview", "evidence", "thesis", "valuation"];
    const labels = { overview: "研究概览", evidence: "证据台账", thesis: "论点树", valuation: "财务估值" };
    const panel = $(`<div class="result-panel">
      <div class="result-tabs">${tabs.map((t, i) => `<div class="result-tab ${i === 0 ? "active" : ""}" data-tab="${t}">${labels[t]}</div>`).join("")}</div>
      <div class="result-view" id="result-view"></div>
    </div>`);
    const view = panel.querySelector("#result-view");
    view.appendChild(renderOverview(ov));
    panel.querySelectorAll(".result-tab").forEach((t) => {
      t.addEventListener("click", () => {
        panel.querySelectorAll(".result-tab").forEach((x) => x.classList.remove("active"));
        t.classList.add("active");
        view.innerHTML = "";
        const key = t.dataset.tab;
        if (key === "overview") view.appendChild(renderOverview(ov));
        if (key === "evidence") view.appendChild(renderEvidence(sample.evidence));
        if (key === "thesis") view.appendChild(renderThesis(sample.thesis));
        if (key === "valuation") view.appendChild(renderValuation(sample.valuation));
      });
    });
    return panel;
  }

  function renderOverview(ov) {
    const profile = Object.entries(ov.profile).map(([k, v]) => `<div class="stat-box"><div class="k">${k}</div><div class="v" style="font-size:var(--fs-sm)">${v}</div></div>`).join("");
    const el = $(`<div style="display:flex;flex-direction:column;gap:var(--space-4)">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <div><b style="font-size:var(--fs-xl);color:var(--text-strong)">${ov.name}</b> <span class="t-muted fs-sm">${ov.code}</span></div>
        <span class="rating-badge rating-${ov.rating}">可研究性 ${ov.rating} 级</span>
        <span class="chip chip-accent">置信度 ${(ov.confidence * 100).toFixed(0)}%</span>
      </div>
      <div class="overview-grid">${profile}</div>
      <div class="card" style="padding:var(--space-4)">
        <div class="t-secondary fs-sm" style="margin-bottom:6px">一句话结论</div>
        <div style="line-height:1.7;color:var(--text-primary)">${ov.conclusion}</div>
      </div>
      ${ov.injectedCog && ov.injectedCog.length ? `<div class="fs-xs t-muted">本次研究已向上下文注入 ${ov.injectedCog.length} 条个人认知 →</div>` : ""}
    </div>`);
    return el;
  }

  function renderEvidence(rows) {
    const body = rows.map((r) => `<tr>
      <td>${r.claim}</td>
      <td><span class="claim-type ct-${r.type}">${typeLabel(r.type)}</span></td>
      <td>${r.ev}</td>
      <td>${r.counter}</td>
      <td><span class="chip chip-green">${(r.conf * 100).toFixed(0)}%</span></td>
      <td class="t-muted">${r.src}</td>
    </tr>`).join("");
    return $(`<table class="evidence-table">
      <thead><tr><th>结论</th><th>类型</th><th>证据</th><th>反证</th><th>置信度</th><th>来源</th></tr></thead>
      <tbody>${body}</tbody></table>`);
  }

  function renderThesis(nodes) {
    const cls = { core: "node-core", support: "node-support", against: "node-against", key: "node-key" };
    const body = nodes.map((n) => `<div class="node ${cls[n.kind]}"><div class="nt">${n.t}</div>${n.d ? `<div class="nd">${n.d}</div>` : ""}</div>`).join("");
    return $(`<div class="thesis-tree">${body}</div>`);
  }

  function renderValuation(v) {
    const metrics = v.metrics.map((m) => `<div class="stat-box"><div class="k">${m.k}</div><div class="v">${m.v}</div></div>`).join("");
    const bars = v.metrics.slice(0, 3).map((m, i) => {
      const pct = 40 + i * 18;
      const colors = ["var(--color-primary)", "var(--color-success)", "var(--color-accent)"];
      return `<div class="val-bar"><div style="display:flex;justify-content:space-between;font-size:var(--fs-xs);color:var(--text-secondary)"><span>${m.k}</span><span>${m.v}</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:linear-gradient(135deg,${colors[i]},${colors[i]})">${pct}%</div></div></div>`;
    }).join("");
    const scen = v.scenarios.map((s) => `<div class="scenario ${s.cls}"><div class="fs-sm t-secondary">${s.name}</div><div class="sv">${s.range}</div></div>`).join("");
    return $(`<div>
      <div class="val-grid">${metrics}</div>
      <div style="margin-bottom:var(--space-4)">${bars}</div>
      <div class="fs-sm fw-600 t-strong" style="margin-bottom:10px">多情景估值区间（元 / 股）</div>
      <div class="val-scenarios">${scen}</div>
    </div>`);
  }

  /* ----- 认知沉淀条 ----- */
  function buildDeposit(sample) {
    const d = sample.deposit || { title: "本次研究要点", content: sample.overview.conclusion };
    const el = $(`<div class="cog-deposit">
      <div class="head">${STAR} 认知沉淀 · 把这次判断存进你的认知库</div>
      <div class="fs-sm t-secondary">${d.title}</div>
      <div class="actions">
        <button class="btn btn-accent btn-sm" id="dep-save">${STAR} 加入我的认知库</button>
        <button class="btn btn-ghost btn-sm" id="dep-edit">我有不同看法</button>
      </div>
    </div>`);
    el.querySelector("#dep-save").addEventListener("click", () => {
      const item = {
        id: "c" + Date.now(), title: d.title, content: d.content,
        type: "自定义", tags: ["#" + sample.overview.name], source: "ai",
        createdAt: today(), updatedAt: today(), injected: true
      };
      Yiyu.store.addCog(item);
      Yiyu.modal.toast("已存入认知库 · " + d.title, "success");
    });
    el.querySelector("#dep-edit").addEventListener("click", () => {
      Yiyu.modal.open({
        title: "记录你的不同看法", bodyHTML: `
          <div class="field"><label>我的观点</label><textarea class="input textarea" id="edit-content" placeholder="写下你与 AI 不同的判断或补充...">${d.content}</textarea></div>`,
        footerHTML: `<button class="btn btn-ghost" id="cancel">取消</button><button class="btn btn-accent" id="save">保存</button>`,
        onMount(root) {
          root.querySelector("#cancel").onclick = Yiyu.modal.close;
          root.querySelector("#save").onclick = () => {
            const v = root.querySelector("#edit-content").value.trim();
            Yiyu.store.addCog({ id: "c" + Date.now(), title: "我的补充：" + d.title.slice(0, 12), content: v || d.content, type: "自定义", tags: ["#" + sample.overview.name], source: "manual", createdAt: today(), updatedAt: today(), injected: true });
            Yiyu.modal.close(); Yiyu.modal.toast("已保存你的观点", "success");
          };
        }
      });
    });
    return el;
  }

  /* ----- 解析研究样本 ----- */
  function resolveSample(query) {
    const q = query || "";
    const keys = Object.keys(Yiyu.data.researchSample);
    for (const k of keys) { if (q.includes(k) || (k === "贵州茅台" && (q.includes("茅台") || q.includes("600519")))) return Yiyu.data.researchSample[k]; }
    return buildGeneric(q);
  }

  function buildGeneric(query) {
    const name = extractName(query);
    return {
      overview: {
        name, code: "—", rating: "B",
        profile: { "行业": "待测", "商业模式": "待测", "生命周期": "待测", "盈利状态": "待测", "收入类型": "待测", "监管": "待测" },
        conclusion: `关于「${name}」的研究：基于公开资料与你的认知库，已形成初步判断。本样本为原型演示数据，完整研究将覆盖画像、证据、估值与风险。`,
        confidence: 0.6, injectedCog: Yiyu.store.getCognition().filter((c) => c.injected).slice(0, 2).map((c) => c.id)
      },
      thinking: [
        { t: "意图识别与实体锁定", d: "解析提问 → 锁定标的：" + name, done: true },
        { t: "可研究性评级", d: "信息丰富度评估中 → 暂定 B 级（演示数据）", done: true },
        { t: "策略路由", d: "按行业画像命中对应策略包", done: true },
        { t: "多角色并行取证", d: "四角色同步采集证据", done: true },
        { t: "证据质检与计算校验", d: "关键指标双源验证", done: true },
        { t: "论点与估值形成", d: "生成论点树 + 估值区间", done: true }
      ],
      evidence: [
        { claim: name + " 的核心竞争力", type: "judgment", ev: "（演示）需结合行业框架进一步取证", counter: "—", conf: 0.6, src: "mock" },
        { claim: "盈利质量与现金流", type: "estimate", ev: "（演示）等待取数", counter: "—", conf: 0.5, src: "mock" }
      ],
      thesis: [
        { kind: "core", t: "核心论点（演示）", d: "完整研究将给出明确论点与反证" },
        { kind: "support", t: "支持证据（演示）", d: "—" },
        { kind: "key", t: "关键假设与跟踪指标", d: "—" }
      ],
      valuation: {
        metrics: [{ k: "PE-TTM", v: "—" }, { k: "ROE", v: "—" }, { k: "毛利率", v: "—" }, { k: "股息率", v: "—" }],
        scenarios: [{ name: "悲观", range: "—", cls: "pessimistic" }, { name: "中性", range: "—", cls: "neutral" }, { name: "乐观", range: "—", cls: "optimistic" }]
      },
      deposit: { title: name + " 研究要点", content: "（演示）本次研究的个性化判断，可沉淀为你的认知。" }
    };
  }

  function extractName(q) {
    let s = (q || "").replace(/(研究一下|分析一下|帮我看下|研究|分析下|分析|研究下|看看|查一下|问一下)/g, "");
    s = s.replace(/(的护城河|怎么样|如何|贵不贵|还能撑多久|估值|的估值|是否稳固|吗|\?|？|。|\.|的)/g, "").trim();
    if (!s) s = "某上市公司";
    return s.slice(0, 10);
  }

  /* ----- helpers ----- */
  function typeLabel(t) { return { fact: "事实", estimate: "估算", judgment: "判断" }[t] || t; }
  function today() { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
  function escapeHTML(s) { return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  /* ----- 历史回放 -----
     后端现在返回真实消息列表；有 sid 时不再渲染欢迎页，直接按顺序重放。 */
  function showHistoryLoading() {
    scrollEl.innerHTML = `<div class="history-loading">
      <span class="typing-dots"><i></i><i></i><i></i></span>
      <span class="fs-sm t-muted">正在打开历史对话…</span>
    </div>`;
  }

  function showHistoryError() {
    scrollEl.innerHTML = `<div class="research-fail">
      <div class="fail-icon">⚠</div>
      <div class="fail-msg">没能打开这条历史对话</div>
      <div class="fail-hint fs-xs t-muted">会话可能已被删除，或不属于当前登录账号。</div>
      <button class="btn btn-accent btn-sm" id="history-back">返回新对话</button>
    </div>`;
    const btn = scrollEl.querySelector("#history-back");
    if (btn) btn.addEventListener("click", () => Yiyu.router.go("chat"));
  }

  /* 回放提示：只说「在查看历史」，不宣称已恢复上下文——
     本轮回答不会自动引用上文结论，除非后端真正注入了历史。 */
  function addHistoryNote(title, count) {
    const tip = document.createElement("div");
    tip.className = "session-restore-note fs-xs t-muted";
    tip.innerHTML = `↩ 正在查看历史对话${title ? "：" + escapeHTML(title) : ""}（${count} 条消息）。继续提问会追加到这条记录。`;
    scrollEl.insertBefore(tip, scrollEl.firstChild);
  }

  function replayMessages(msgs) {
    scrollEl.innerHTML = "";
    let lastQuery = "";
    msgs.forEach((m) => {
      if (m.role === "user") {
        lastQuery = m.content || "";
        addUserMsg(lastQuery);
        return;
      }
      const meta = m.metadata || {};
      renderReport(addAIMsg(), m.content || "", meta.citations || [], lastQuery, meta);
    });
    scrollBottom();
  }

  async function openHistory(sid) {
    showHistoryLoading();
    try {
      const r = await Yiyu.api.getSession(sid);
      const msgs = (r && r.messages) || [];
      if (!msgs.length) {
        pendingSessionId = null;
        scrollEl.innerHTML = welcome();
        return;
      }
      replayMessages(msgs);
      addHistoryNote(r.title, msgs.length);
    } catch (e) {
      pendingSessionId = null;
      showHistoryError();
    }
  }

  /* ----- 页面入口 ----- */
  function render(view, topbarEl) {
    view.style.flexDirection = "column";
    topbar(topbarEl);
    view.innerHTML = `<div class="chat-scroll" id="chat-scroll"></div><div class="composer" id="composer"></div>`;
    scrollEl = document.getElementById("chat-scroll");
    composerEl = document.getElementById("composer");
    composerEl.innerHTML = composerHTML();

    // 从历史记录进入：读取 ?sid=（hash 路由下 query 在 hash 内）
    pendingSessionId = null;
    const sid = new URLSearchParams(location.hash.split("?")[1] || "").get("sid");
    if (sid) {
      pendingSessionId = sid;   // 后续提问沿用同一会话，消息追加不覆盖
      openHistory(sid);
    } else {
      scrollEl.innerHTML = welcome();
    }

    const input = document.getElementById("chat-input");
    const send = document.getElementById("send-btn");
    const doSend = () => {
      const v = input.value.trim();
      if (!v) return;
      runResearch(v);
    };
    send.addEventListener("click", doSend);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
    });
    input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 120) + "px"; });

    /* 欢迎页引导问句点击 */
    scrollEl.querySelectorAll('.guide-item, .qa-item').forEach(el => {
      el.addEventListener('click', () => {
        const q = el.dataset.q;
        if (q) { input.value = q; input.focus(); doSend(); }
      });
    });
  }

  function reset() {
    pendingSessionId = null;
    const sc = document.getElementById("chat-scroll");
    if (sc) sc.innerHTML = welcome();
    if (Yiyu.editor && Yiyu.editor.applyOverrides) Yiyu.editor.applyOverrides(sc);
  }

  Yiyu.chat = { render, reset };
})();
