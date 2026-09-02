/* ============================================================
   交易复盘 占位页
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});
  const DOC = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h6"/></svg>';
  const I1 = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M19 9l-5 5-4-4-3 3"/></svg>';
  const I2 = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 1 0 10 10"/><path d="M12 6v6l4 2"/></svg>';
  const I3 = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3 8-8"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>';
  const I4 = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';

  function topbar(t) {
    t.innerHTML = `<div class="page-title"><b data-edit-id="rev-top-title" data-edit-label="交易复盘页标题">交易复盘</b><span class="sub" data-edit-id="rev-top-sub" data-edit-label="交易复盘页副标题">决策回顾与方法论沉淀</span></div>
      <div class="topbar-right"><div class="avatar">投</div></div>`;
  }

  function render(view, topbarEl) {
    view.style.flexDirection = "column";
    topbar(topbarEl);
    view.innerHTML = `
      <div class="placeholder">
        <div class="ph-icon">${DOC}</div>
        <h1 data-edit-id="rev-title" data-edit-label="交易复盘大标题">交易复盘</h1>
        <p class="desc" data-edit-id="rev-desc" data-edit-label="交易复盘说明">回顾每一笔交易决策的前因后果，对照当初的研究逻辑与认知，把成功经验与失误教训沉淀为可复用的投资方法论。</p>
        <div class="soon-badge">🚧 即将上线 · Coming Soon</div>
        <div class="feature-cards">
          <div class="feature-card card"><div class="fc-icon" style="background:linear-gradient(135deg,var(--color-primary),var(--color-primary-soft))">${I1}</div><div class="fc-title">决策时间线</div><div class="fc-desc">买卖点、当时逻辑、触发信号的完整回放。</div></div>
          <div class="feature-card card"><div class="fc-icon" style="background:linear-gradient(135deg,var(--color-success),#34D399)">${I2}</div><div class="fc-title">逻辑对照</div><div class="fc-desc">买入逻辑兑现了没？当初的关键假设是否被推翻。</div></div>
          <div class="feature-card card"><div class="fc-icon" style="background:linear-gradient(135deg,var(--color-accent),var(--color-accent-soft))">${I3}</div><div class="fc-title">盈亏归因</div><div class="fc-desc">区分运气与能力，标注认知与执行层面的得失。</div></div>
          <div class="feature-card card"><div class="fc-icon" style="background:linear-gradient(135deg,var(--color-info),#22D3EE)">${I4}</div><div class="fc-title">方法论沉淀</div><div class="fc-desc">把复盘结论一键写入认知库，反哺下一次研究。</div></div>
        </div>
        <div class="ph-subscribe">
          <input class="input" placeholder="留下邮箱，上线第一时间通知你"/>
          <button class="btn btn-primary">订阅更新</button>
        </div>
      </div>`;
  }
  Yiyu.review = { render };
})();
