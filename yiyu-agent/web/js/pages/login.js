/* 访客登录弹窗：邮箱验证码是唯一真实可用的登录方式。 */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});
  let pending = null;
  let cooldownTimer = null;

  function escapeHTML(s) { return (s || "").replace(/[&<>\"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  function open() {
    if (Yiyu.api.isAuthed()) return Promise.resolve(true);
    if (pending) return pending;
    pending = new Promise((resolve) => {
      const gate = document.getElementById("login-gate");
      let settled = false, onKey;
      const finish = (ok) => {
        if (settled) return;
        settled = true;
        clearInterval(cooldownTimer); if (onKey) document.removeEventListener("keydown", onKey);
        gate.classList.remove("show"); gate.innerHTML = ""; pending = null;
        if (ok && Yiyu.sidebar) { Yiyu.sidebar.render(); if (Yiyu.app) Yiyu.app.refreshHistory(); }
        resolve(ok);
      };
      gate.innerHTML = `
        <div class="login-overlay" id="login-overlay">
          <section class="login-dialog" role="dialog" aria-modal="true" aria-labelledby="login-title">
            <header class="login-dialog-head"><div class="login-brand"><img src="assets/logo.svg" alt=""/>以渔</div><button class="login-close" id="login-close" aria-label="关闭登录">×</button></header>
            <div class="login-dialog-body">
              <h1 class="login-dialog-title" id="login-title">登录 / 注册</h1>
              <p class="login-dialog-desc">登录后即可发起研究、保存历史和管理你的认知。</p>
              <div class="login-form">
                <div class="field"><label for="gate-email">邮箱</label><input class="input" id="gate-email" type="email" autocomplete="email" placeholder="you@example.com" value="${escapeHTML(Yiyu.api.getEmail())}"/></div>
                <div class="field"><label for="gate-code">验证码</label><div class="login-code-row"><input class="input" id="gate-code" type="text" inputmode="numeric" maxlength="6" autocomplete="one-time-code" placeholder="6 位验证码"/><button class="btn" id="gate-send">获取验证码</button></div></div>
                <div class="login-hint" id="gate-hint" aria-live="polite"></div>
                <button class="login-submit" id="gate-submit">登录 / 注册</button>
              </div>
              <p class="login-disclaimer">登录即表示同意《以渔用户协议》与《隐私政策》。<br/>以渔不提供投资建议，不预测涨跌。</p>
            </div>
          </section>
        </div>`;
      const email = gate.querySelector("#gate-email");
      const code = gate.querySelector("#gate-code");
      const hint = gate.querySelector("#gate-hint");
      const send = gate.querySelector("#gate-send");
      const submit = gate.querySelector("#gate-submit");
      const message = (text, kind) => { hint.textContent = text; hint.className = "login-hint" + (kind ? " " + kind : ""); };
      const close = () => finish(false);
      gate.querySelector("#login-close").onclick = close;
      gate.querySelector("#login-overlay").onclick = (e) => { if (e.target.id === "login-overlay") close(); };
      onKey = (e) => { if (e.key === "Escape") close(); };
      document.addEventListener("keydown", onKey);
      send.onclick = async () => {
        const value = email.value.trim();
        if (!/^[^@\s]+@[^@\s]+$/.test(value)) return message("请输入有效邮箱", "error");
        send.disabled = true; send.textContent = "发送中…";
        try {
          const data = await Yiyu.api.sendCode(value);
          if (data.dev_code) { code.value = data.dev_code; message("开发模式：验证码已自动填入", "success"); }
          else message("验证码已发送，5 分钟内有效", "success");
          let seconds = 60;
          clearInterval(cooldownTimer);
          cooldownTimer = setInterval(() => { seconds -= 1; send.textContent = seconds > 0 ? seconds + "s 后重发" : "获取验证码"; if (seconds <= 0) { clearInterval(cooldownTimer); send.disabled = false; } }, 1000);
        } catch (e) { send.disabled = false; send.textContent = "获取验证码"; message(e.message || "发送失败", "error"); }
      };
      submit.onclick = async () => {
        const mailbox = email.value.trim(), verifyCode = code.value.trim();
        if (!mailbox || !verifyCode) return message("请填写邮箱与验证码", "error");
        submit.disabled = true; submit.textContent = "验证中…";
        try { await Yiyu.api.verify(mailbox, verifyCode); finish(true); }
        catch (e) { submit.disabled = false; submit.textContent = "登录 / 注册"; message(e.message || "登录失败", "error"); }
      };
      email.onkeydown = (e) => { if (e.key === "Enter") code.focus(); };
      code.onkeydown = (e) => { if (e.key === "Enter") submit.click(); };
      gate.classList.add("show"); email.focus();
    });
    return pending;
  }
  Yiyu.login = { open };
})();
