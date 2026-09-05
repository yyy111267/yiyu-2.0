/* ============================================================
   通用模态框 (window.Yiyu.modal)
   api.open({ title, bodyHTML, footerHTML, onMount }) -> returns close()
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});

  function close() {
    const root = document.getElementById("modal-root");
    root.innerHTML = "";
  }

  function open({ title, bodyHTML, footerHTML, onMount }) {
    const root = document.getElementById("modal-root");
    root.innerHTML = `
      <div class="modal-overlay" id="modal-overlay">
        <div class="modal" role="dialog">
          <div class="modal-head">
            <b style="font-size:var(--fs-lg);color:var(--text-strong)">${title}</b>
            <button class="btn-icon" id="modal-close" aria-label="关闭">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
            </button>
          </div>
          <div class="modal-body">${bodyHTML}</div>
          ${footerHTML ? `<div class="modal-foot">${footerHTML}</div>` : ""}
        </div>
      </div>`;
    document.getElementById("modal-close").addEventListener("click", close);
    document.getElementById("modal-overlay").addEventListener("click", (e) => {
      if (e.target.id === "modal-overlay") close();
    });
    document.addEventListener("keydown", escClose);
    if (onMount) onMount(root);
    return close;
  }

  function escClose(e) { if (e.key === "Escape") close(); }

  /* Toast 提示 */
  function toast(msg, type) {
    const root = document.getElementById("toast-root");
    const t = document.createElement("div");
    t.className = "toast " + (type || "");
    t.textContent = msg;
    root.appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transform = "translateY(8px)"; t.style.transition = "all .3s"; }, 1600);
    setTimeout(() => t.remove(), 2000);
  }

  Yiyu.modal = { open, close, toast };
})();
