/* ============================================================
   持仓追踪 占位页
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});
  const CHART = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M19 9l-5 5-4-4-3 3"/></svg>';
  const I1 = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l3-3 3 3 4-5"/></svg>';
  const I2 = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
  const I3 = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l3 7h7l-5.5 4 2 7L12 17l-6.5 3 2-7L2 9h7z"/></svg>';
  const I4 = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z"/><path d="M4 9h16M9 4v16"/></svg>';

  function topbar(t) {
    t.innerHTML = `<div class="page-title"><b data-edit-id="hold-top-title" data-edit-label="持仓追踪页标题">持仓追踪</b><span class="sub" data-edit-id="hold-top-sub" data-edit-label="持仓追踪页副标题">组合管理与风险监控</span></div>
      <div class="topbar-right"><div class="avatar">投</div></div>`;
  }

  function render(view, topbarEl) {
    view.style.flexDirection = "column";
    topbar(topbarEl);
    view.innerHTML = `
      <div class="placeholder">
        <div class="ph-icon">${CHART}</div>
        <h1 data-edit-id="hold-title" data-edit-label="持仓追踪大标题">持仓追踪</h1>
        <p class="desc" data-edit-id="hold-desc" data-edit-label="持仓追踪说明">管理和跟踪你的投资组合持仓，实时监控盈亏、成本与风险暴露，并与认知库联动校验持仓逻辑是否仍然成立。</p>
        <div class="soon-badge">🚧 即将上线 · Coming Soon</div>
        <div class="feature-cards">
          <div class="feature-card card"><div class="fc-icon" style="background:linear-gradient(135deg,var(--color-primary),var(--color-primary-soft))">${I1}</div><div class="fc-title">持仓总览</div><div class="fc-desc">组合净值、成本、实时盈亏与行业分布一目了然。</div></div>
          <div class="feature-card card"><div class="fc-icon" style="background:linear-gradient(135deg,var(--color-success),#34D399)">${I2}</div><div class="fc-title">动态再平衡</div><div class="fc-desc">当仓位偏离认知纪律时，主动提示减仓或加仓。</div></div>
          <div class="feature-card card"><div class="fc-icon" style="background:linear-gradient(135deg,var(--color-accent),var(--color-accent-soft))">${I3}</div><div class="fc-title">逻辑校验</div><div class="fc-desc">对照认知库，检查持仓是否仍符合你的投资框架。</div></div>
          <div class="feature-card card"><div class="fc-icon" style="background:linear-gradient(135deg,var(--color-info),#22D3EE)">${I4}</div><div class="fc-title">风险预警</div><div class="fc-desc">集中度、回撤、关键假设被证伪的实时提醒。</div></div>
        </div>
        <div class="ph-subscribe">
          <input class="input" placeholder="留下邮箱，上线第一时间通知你"/>
          <button class="btn btn-primary">订阅更新</button>
        </div>
      </div>`;
  }
  Yiyu.holdings = { render };
})();
