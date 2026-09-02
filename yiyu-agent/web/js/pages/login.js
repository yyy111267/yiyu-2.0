/* ============================================================
   登录门禁 (window.Yiyu.login) — 全屏登录页
   未登录时覆盖主界面，登录成功后由 app.js 拉起应用。
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});

  let cooldownTimer = null;

  function render(opts) {
    const gate = document.getElementById("login-gate");
    if (!gate) return;
    gate.innerHTML = `
      <div class="login-card">
        <div class="login-logo">
          <img src="assets/logo-cat.png" alt="以渔" onerror="this.style.display='none'"/>
        </div>
        <h1 class="login-title">以渔</h1>
        <p class="login-sub">投研认知陪练</p>

        <div class="login-form">
          <div class="field">
            <label>邮箱</label>
            <input class="input" id="gate-email" type="email" placeholder="you@example.com"
                   autocomplete="email" value="${escapeHTML(Yiyu.api.getEmail() || "")}"/>
          </div>
          <div class="field">
            <label>验证码</label>
            <div class="code-row">
              <input class="input" id="gate-code" type="text" inputmode="numeric" maxlength="6"
                     placeholder="6 位验证码" autocomplete="one-time-code"/>
              <button class="btn btn-ghost" id="gate-send">发送验证码</button>
            </div>
            <div class="login-hint" id="gate-hint"></div>
          </div>
          <button class="btn btn-accent btn-block" id="gate-submit">登录 / 注册</button>
        </div>

        <p class="login-disclaimer">登录即表示同意《以渔用户协议》与《隐私政策》。<br/>以渔是认知陪练工具，不提供投资建议，不预测涨跌。</p>
      </div>`;

    const emailEl = gate.querySelector("#gate-email");
    const codeEl = gate.querySelector("#gate-code");
    const sendBtn = gate.querySelector("#gate-send");
    const submitBtn = gate.querySelector("#gate-submit");
    const hint = gate.querySelector("#gate-hint");

    sendBtn.addEventListener("click", async () => {
      const email = emailEl.value.trim();
      if (!/^[^@\s]+@[^@\s]+$/.test(email)) { hint.textContent = "请输入有效邮箱"; hint.className = "login-hint error"; return; }
      sendBtn.disabled = true; sendBtn.textContent = "发送中…";
      try {
        const d = await Yiyu.api.sendCode(email);
        if (d.dev_code) {
          codeEl.value = d.dev_code;
          hint.textContent = "开发模式：验证码已自动填入，点「登录」即可";
        } else {
          hint.textContent = "验证码已发送，5 分钟内有效";
        }
        hint.className = "login-hint success";
        cooldown(60, sendBtn);
      } catch (e) {
        hint.textContent = e.message || "发送失败";
        hint.className = "login-hint error";
        sendBtn.disabled = false; sendBtn.textContent = "重新发送";
      }
    });

    submitBtn.addEventListener("click", async () => {
      const email = emailEl.value.trim();
      const code = codeEl.value.trim();
      if (!email || !code) { hint.textContent = "请填写邮箱与验证码"; hint.className = "login-hint error"; return; }
      submitBtn.disabled = true; submitBtn.textContent = "验证中…";
      try {
        await Yiyu.api.verify(email, code);
        hint.textContent = "登录成功，正在进入…";
        hint.className = "login-hint success";
        setTimeout(() => {
          gate.classList.remove("show");
          gate.innerHTML = "";
          if (opts && opts.onSuccess) opts.onSuccess();
        }, 300);
      } catch (e) {
        hint.textContent = e.message || "登录失败";
        hint.className = "login-hint error";
        submitBtn.disabled = false; submitBtn.textContent = "登录 / 注册";
      }
    });

    codeEl.addEventListener("keydown", (e) => { if (e.key === "Enter") submitBtn.click(); });
    emailEl.addEventListener("keydown", (e) => { if (e.key === "Enter") codeEl.focus(); });

    gate.classList.add("show");
    emailEl.focus();
  }

  function cooldown(sec, btn) {
    let s = sec;
    btn.textContent = s + "s 后重发";
    clearInterval(cooldownTimer);
    cooldownTimer = setInterval(() => {
      s--;
      if (s <= 0) { clearInterval(cooldownTimer); btn.disabled = false; btn.textContent = "发送验证码"; }
      else btn.textContent = s + "s 后重发";
    }, 1000);
  }

  function escapeHTML(s) { return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  Yiyu.login = { render };
})();
