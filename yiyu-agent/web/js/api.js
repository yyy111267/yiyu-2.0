/* ============================================================
   后端 API 客户端 (window.Yiyu.api)
   - 邮箱验证码登录（token 存 localStorage）
   - SSE 流式聊天（fetch + ReadableStream，逐帧解析）
   - 401 自动弹登录
   后端协议（api/routes/chat.py）：
     POST /api/v1/chat  {message, session_id?, skill?}  Bearer token
     SSE 帧：event: message / data: {type, content, metadata, timestamp}
   事件类型：accepted / routing / preloop_progress / preloop /
     thought / tool_call / tool_result / answer_delta / warning /
     error / final_answer / ask_confirmation / complete
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});

  const TOKEN_KEY = "yiyu_token";
  const EMAIL_KEY = "yiyu_email";
  const DEMO_TOKEN = "yiyu_demo_cognition_preview";
  // 同域部署留空（走相对路径）；本地分端口调试时改为 http://127.0.0.1:8000
  const API_BASE = "";

  function getToken() { return localStorage.getItem(TOKEN_KEY) || ""; }
  function getEmail() { return localStorage.getItem(EMAIL_KEY) || ""; }
  function isAuthed() { return !!getToken(); }
  function isDemo() { return getToken() === DEMO_TOKEN; }
  function demoRecords() {
    if (!Yiyu.data.demoMemory) {
      Yiyu.data.demoMemory = (Yiyu.data.cognitionSeed || []).map((item, index) => {
        const tags = (item.tags || []).map((tag) => tag.replace(/^#/, ""));
        const industry = item.type === "行业认知" ? tags[0] : "";
        return {
          id: "demo-" + (index + 1), statement: item.title, content: item.content,
          category: item.type === "行业认知" ? "自定义" : item.type,
          subject_scope: industry ? "industry" : "general",
          status: item.injected ? "active" : "archived", scope: industry,
          source: item.source === "ai" ? "research_extract" : "manual",
          first_seen_at: item.createdAt, updated_at: item.updatedAt,
        };
      });
    }
    return Yiyu.data.demoMemory;
  }
  function startDemo() {
    localStorage.setItem(TOKEN_KEY, DEMO_TOKEN);
    localStorage.setItem(EMAIL_KEY, "demo@yiyu.local");
    demoRecords();
  }
  function logout() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(EMAIL_KEY);
    /* 通知应用层回到登录门禁（app.js 监听） */
    window.dispatchEvent(new Event("yiyu:logout"));
  }

  function authHeaders(extra) {
    const h = extra || {};
    if (getToken()) h["Authorization"] = "Bearer " + getToken();
    return h;
  }

  async function _request(path, opts) {
    const options = opts || {};
    const res = await fetch(API_BASE + path, {
      method: options.method || "GET",
      headers: authHeaders(options.headers || {}),
      body: options.body === undefined ? undefined : JSON.stringify(options.body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) logout();
    if (!res.ok) {
      const err = new Error(data.detail || ("HTTP " + res.status));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  async function _post(path, body, authed) {
    const headers = { "Content-Type": "application/json" };
    const request = { method: "POST", headers, body };
    if (authed === false) {
      const res = await fetch(API_BASE + path, {
        method: "POST",
        headers,
        body: JSON.stringify(body || {}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const err = new Error(data.detail || ("HTTP " + res.status));
        err.status = res.status;
        throw err;
      }
      return data;
    }
    return _request(path, request);
  }

  /* ---------- 登录 ---------- */
  async function sendCode(email) {
    return _post("/api/v1/auth/send_code", { email }, false);
  }

  async function verify(email, code) {
    const data = await _post("/api/v1/auth/verify", { email, code }, false);
    if (data.token) {
      localStorage.setItem(TOKEN_KEY, data.token);
      localStorage.setItem(EMAIL_KEY, email);
    }
    return data;
  }

  /* 登录 UI 只在 pages/login.js 维护；API 层不再复制一份表单。 */
  function ensureLogin() { return Yiyu.login.open(); }

  /* ---------- SSE 流式聊天 ----------
     opts: {message, sessionId, skill, onEvent(evt), signal}
     onEvent 收到的是解析后的 JSON 帧 {type, content, metadata, timestamp}。
     返回 {ok, error}；网络/HTTP 错误不会抛出到调用方（已在内部兜底）。 */
  async function streamChat(opts) {
    const onEvent = opts.onEvent || function () {};
    let res;
    try {
      res = await fetch(API_BASE + "/api/v1/chat", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" }),
        body: JSON.stringify({
          message: opts.message,
          session_id: opts.sessionId || undefined,
          skill: opts.skill || undefined,
        }),
        signal: opts.signal,
      });
    } catch (e) {
      if (e.name === "AbortError") return { ok: false, error: "已取消" };
      return { ok: false, error: "网络异常：" + e.message };
    }

    if (res.status === 401) {
      logout();
      onEvent({ type: "error", content: "登录已过期，请重新登录", metadata: {} });
      return { ok: false, error: "unauthorized", needLogin: true };
    }
    if (res.status === 429) {
      const d = await res.json().catch(() => ({}));
      onEvent({ type: "error", content: d.detail || "今日研究次数已达上限", metadata: d });
      return { ok: false, error: "rate_limited" };
    }
    if (!res.ok || !res.body) {
      onEvent({ type: "error", content: "服务异常（HTTP " + res.status + "）", metadata: {} });
      return { ok: false, error: "http_" + res.status };
    }

    // 逐行解析 SSE：event: / data: 帧，空行分发
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let eventName = "message";
    let dataLines = [];

    function dispatch() {
      if (!dataLines.length) return;
      const raw = dataLines.join("\n");
      dataLines = []; eventName = "message";
      let evt;
      try { evt = JSON.parse(raw); } catch (e) { return; }
      if (evt && typeof evt === "object") onEvent(evt);
    }

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        // SSE 帧以空行分隔（\n\n 或 \r\n\r\n）
        while ((idx = buf.search(/\r?\n\r?\n/)) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx).replace(/^\r?\n\r?\n/, "");
          for (const line of block.split(/\r?\n/)) {
            if (line.startsWith("event:")) eventName = line.slice(6).trim();
            else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
            // 忽略 id:/retry:/注释行
          }
          dispatch();
        }
      }
      // flush 残余
      if (buf.trim()) {
        for (const line of buf.split(/\r?\n/)) {
          if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
        }
        dispatch();
      }
      return { ok: true };
    } catch (e) {
      if (e.name === "AbortError") return { ok: false, error: "已取消" };
      return { ok: false, error: "流中断：" + e.message };
    }
  }

  /* 认知确认卡入库（ask_confirmation → 用户确认/编辑后调用） */
  async function confirmMemory(cards, sourceTaskId) {
    if (isDemo()) {
      const saved = [];
      (cards || []).forEach((card) => {
        const candidate = card.candidate || {};
        const item = { id: "demo-" + Date.now() + "-" + saved.length, statement: candidate.statement || "未命名认知", content: candidate.content || "", category: candidate.category || "自定义", subject_scope: candidate.subject_scope || "general", symbol: candidate.symbol || "", status: candidate.status || "active", scope: candidate.scope || "", source: "manual", first_seen_at: new Date().toISOString(), updated_at: new Date().toISOString() };
        demoRecords().unshift(item); saved.push({ saved: true, id: item.id });
      });
      return { results: saved };
    }
    return _post("/api/v1/memory/confirm", { cards: cards, source_task_id: sourceTaskId || "" });
  }

  async function listMemory() {
    if (isDemo()) return { items: demoRecords().map((item) => Object.assign({}, item)) };
    return _request("/api/v1/memory/me");
  }

  async function updateMemory(id, fields) {
    if (isDemo()) {
      const item = demoRecords().find((record) => record.id === id);
      if (!item) throw new Error("体验数据不存在");
      Object.assign(item, fields || {}, { updated_at: new Date().toISOString() });
      return { item: Object.assign({}, item) };
    }
    return _request("/api/v1/memory/" + encodeURIComponent(id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: { fields: fields || {} },
    });
  }

  async function deleteMemory(id) {
    if (isDemo()) { Yiyu.data.demoMemory = demoRecords().filter((item) => item.id !== id); return { ok: true }; }
    return _request("/api/v1/memory/" + encodeURIComponent(id), { method: "DELETE" });
  }

  /* 会话持久化与历史恢复：用真实后端替换 mock 历史 */
  async function listSessions() {
    if (isDemo()) return { items: [] };
    return _request("/api/v1/sessions");
  }
  async function getSession(id) {
    return _request("/api/v1/sessions/" + encodeURIComponent(id));
  }
  async function deleteSession(id) {
    return _request("/api/v1/sessions/" + encodeURIComponent(id), { method: "DELETE" });
  }

  Yiyu.api = {
    getToken, getEmail, isAuthed, isDemo, startDemo, logout, authHeaders,
    sendCode, verify, ensureLogin, streamChat, confirmMemory,
    listMemory, updateMemory, deleteMemory,
    listSessions, getSession, deleteSession,
  };
})();
