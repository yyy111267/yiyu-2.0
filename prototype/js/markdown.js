/* ============================================================
   安全 Markdown 渲染器 (window.Yiyu.md)

   安全原则：转义优先。所有文本先 escapeHTML，只输出本文件构造的
   白名单标签，绝不把模型返回的原始 HTML 放进页面。

   支持：标题 / 加粗 / 列表 / 表格 / 引用块 / 分隔线 / 行内代码 /
         链接（仅 http/https）/ 行内来源标记 [e1]
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  function escapeHTML(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC[c]);
  }

  /* 只放行 http/https，杜绝 javascript: / data: 等协议注入 */
  function safeURL(u) {
    const s = String(u || "").trim();
    if (!/^https?:\/\/[^\s]+$/i.test(s)) return "";
    return s.replace(/"/g, "%22");
  }

  /* 关键数字：只加重「带单位的数字」（30.0% / 1,741 亿 / 20.3×）。
     不加颜色——整页都是彩色数字会很碎，用字重 + 等宽数字就够了。 */
  const NUM_RE = /(\d[\d,]*(?:\.\d+)?)\s*(%|％|×|倍|个百分点|pct|亿元|亿|万元|万|元|港元|美元|HKD|CNY|USD)/g;

  /* 最小证据单元：同一句话里来源相同的引用只保留最后一个标记，
     避免「营收 1,741 亿 [1]，净利润 862 亿 [1]」这种逐数字挂引用。 */
  const CITE_ONE = /<button type="button" class="cite-ref" data-ref="([^"]+)">[^<]*<\/button>/g;

  function dedupeRefs(segment) {
    const hits = [];
    let m;
    CITE_ONE.lastIndex = 0;
    while ((m = CITE_ONE.exec(segment))) {
      hits.push({ ref: m[1], start: m.index, end: m.index + m[0].length });
    }
    if (hits.length < 2) return segment;
    const lastOf = {};
    hits.forEach((h, i) => { lastOf[h.ref] = i; });
    let out = "";
    let cursor = 0;
    hits.forEach((h, i) => {
      if (lastOf[h.ref] === i) return;
      let start = h.start;
      while (start > cursor && /[\s　]/.test(segment[start - 1])) start--; // 连标记前的空格一起去掉
      out += segment.slice(cursor, start);
      cursor = h.end;
    });
    return out + segment.slice(cursor);
  }

  /* ---------- 行内解析 ---------- */
  function inline(text, refs) {
    let out = escapeHTML(text);

    // 行内代码：内容不再参与后续行内解析
    out = out.replace(/`([^`]+)`/g, (_m, code) => `<code class="md-code">${code}</code>`);

    // 关键数字（放在链接/引用之前，此时串里只有 code 标签，不会误伤属性）
    out = out.replace(NUM_RE, '<span class="md-num">$1$2</span>');

    // 链接：仅 http/https，强制新窗口且 noopener。
    // URL 段用 [^\s]+ 贪婪到最后一个 )，避免出现 javascript:alert(3) 残留半截括号
    out = out.replace(/\[([^\]]*)\]\(([^\s]+)\)/g, (_m, label, url) => {
      const safe = safeURL(url);
      if (!safe) return label;
      return `<a class="md-link" href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });

    // 加粗
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

    // 来源标记 [1] / [e1]：后面紧跟 ( 的属于链接语法，已在上一步消费
    out = out.replace(/\[(e?\d+)\](?!\()/gi, (_m, id) => {
      const key = normalizeRef(id);
      if (refs) refs.add(key);
      return `<button type="button" class="cite-ref" data-ref="${escapeHTML(key)}">${escapeHTML(refLabel(id))}</button>`;
    });

    // 一句话内同源引用合并（偶数段是句子，奇数段是 。！？；\n 分隔符）
    out = out.split(/([。！？；\n])/).map((seg, i) => (i % 2 ? seg : dedupeRefs(seg))).join("");

    return out;
  }

  /* 引用 id 规范化：E1 / e1 / 1 统一成 e1，便于与后端 evidence_id 对齐 */
  function normalizeRef(id) {
    const s = String(id || "").trim().toLowerCase();
    return /^\d+$/.test(s) ? "e" + s : s;
  }
  function refLabel(id) {
    return String(id || "").replace(/^e/i, "");
  }

  /* ---------- 表格 ---------- */
  function splitRow(line) {
    return String(line).trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  }

  /* 对齐：| ---: | 右对齐（财务数字列用），| :--: | 居中 */
  function parseAlign(delimRow) {
    return splitRow(delimRow).map((cell) => {
      const left = cell.startsWith(":");
      const right = cell.endsWith(":");
      if (left && right) return "center";
      if (right) return "right";
      return "";
    });
  }

  function cellHTML(text, align, tag, refs) {
    const style = align ? ` style="text-align:${align}"` : "";
    return `<${tag}${style}>${inline(text, refs)}</${tag}>`;
  }

  function buildTable(head, rows, refs, align) {
    const at = (i) => (align && align[i]) || "";
    const th = head.map((c, i) => cellHTML(c, at(i), "th", refs)).join("");
    const tb = rows
      .map((r) => `<tr>${r.map((c, i) => cellHTML(c, at(i), "td", refs)).join("")}</tr>`)
      .join("");
    return `<div class="md-table-wrap"><table class="md-table"><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table></div>`;
  }

  /* 判断一行是否是新块级的开始（段落累积需在此停下） */
  function isBlockStart(line) {
    return (
      /^\s*#{1,6}\s+/.test(line) ||
      /^\s*>\s?/.test(line) ||
      /^\s*[-*+]\s+/.test(line) ||
      /^\s*\d+[.)]\s+/.test(line) ||
      /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line) ||
      /^\s*```/.test(line) ||
      line.includes("|")
    );
  }

  /* ---------- 块级解析 ---------- */
  function render(md, opts) {
    const refs = (opts && opts.refs) || new Set();
    const lines = String(md == null ? "" : md).replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // 空行
      if (!line.trim()) { i++; continue; }

      // 代码围栏：整段按纯文本转义输出，避免未闭合围栏破坏后续渲染
      if (/^\s*```/.test(line)) {
        const buf = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++; // 跳过结束围栏
        out.push(`<pre class="md-pre"><code>${escapeHTML(buf.join("\n"))}</code></pre>`);
        continue;
      }

      // 分隔线
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        out.push('<hr class="md-hr" />');
        i++;
        continue;
      }

      // 标题
      let m = /^(#{1,6})\s+(.*)$/.exec(line);
      if (m) {
        const lvl = Math.min(m[1].length, 6);
        out.push(`<h${lvl} class="md-h md-h${lvl}">${inline(m[2], refs)}</h${lvl}>`);
        i++;
        continue;
      }

      // 引用块（连续 > 行）
      if (/^\s*>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        out.push(`<blockquote class="md-quote">${inline(buf.join(" "), refs)}</blockquote>`);
        continue;
      }

      // 表格：当前行含 | 且下一行是 |---| 分隔行
      if (
        line.includes("|") &&
        i + 1 < lines.length &&
        lines[i + 1].includes("-") &&
        /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])
      ) {
        const head = splitRow(line);
        const align = parseAlign(lines[i + 1]);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        out.push(buildTable(head, rows, refs, align));
        continue;
      }

      // 无序列表
      if (/^\s*[-*+]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*[-*+]\s+/, ""));
          i++;
        }
        out.push(`<ul class="md-ul">${items.map((t) => `<li>${inline(t, refs)}</li>`).join("")}</ul>`);
        continue;
      }

      // 有序列表
      if (/^\s*\d+[.)]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ""));
          i++;
        }
        out.push(`<ol class="md-ol">${items.map((t) => `<li>${inline(t, refs)}</li>`).join("")}</ol>`);
        continue;
      }

      // 段落：连续非空且非块级开始的行
      const buf = [];
      while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) {
        buf.push(lines[i].trim());
        i++;
      }
      if (buf.length) {
        out.push(`<p class="md-p">${inline(buf.join(" "), refs)}</p>`);
      } else {
        // 兜底：既未归类也未形成段落（例如孤立的一行 |），按纯文本段落输出，防死循环
        out.push(`<p class="md-p">${inline(line, refs)}</p>`);
        i++;
      }
    }

    return {
      html: pruneRefs(out.join("\n"), opts && opts.validRefs),
      refs: Array.from(refs),
    };
  }

  /* validRefs（citations 的 evidence_id 集合）外的引用角标降级为纯文本，
     避免留下点击无反应的死按钮（如引用指向未下发的结构化字段证据）。 */
  function pruneRefs(html, validRefs) {
    if (!validRefs) return html;
    return html.replace(
      /<button type="button" class="cite-ref" data-ref="([^"]+)">([^<]*)<\/button>/g,
      (m, ref, label) => (validRefs.has(ref) ? m : label),
    );
  }

  Yiyu.md = { render, escapeHTML, normalizeRef, refLabel };
})();
