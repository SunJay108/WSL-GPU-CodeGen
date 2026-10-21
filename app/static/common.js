"use strict";

/* Helpers shared by the chat page (app.js) and the admin panel (admin.js). */
window.UI = (() => {
  const ICONS = {
    logo: '<svg viewBox="0 0 24 24"><path d="m8 7-5 5 5 5"/><path d="m16 7 5 5-5 5"/><path d="m14 4-4 16"/></svg>',
    search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><path d="m16 16 4 4"/></svg>',
    edit: '<svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16v4z"/><path d="m13.5 6.5 4 4"/></svg>',
    trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
    copy: '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/></svg>',
    check: '<svg viewBox="0 0 24 24"><path d="m5 12 5 5L20 7"/></svg>',
  };

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const child of children.flat(Infinity)) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const icon = (name) => el("span", { html: ICONS[name], style: "display:contents" });

  /** Called with no arguments when the server answers 401 (not signed in). */
  let onUnauthorized = () => {};

  async function errorText(res) {
    try { return (await res.json()).detail || res.statusText; } catch (_) { return res.statusText; }
  }

  async function request(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 401 && !url.startsWith("/api/auth/")) onUnauthorized();
    if (!res.ok) {
      const err = new Error(await errorText(res));
      err.status = res.status;
      throw err;
    }
    return res;
  }

  const api = async (method, url, body) => (await request(method, url, body)).json();

  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ------------------------------------------------------------ Markdown

  /** Inline formatting; code spans are cut out first so nothing inside them is formatted. */
  function inline(text) {
    return text.split(/(`[^`]+`)/g).map((part) => {
      if (/^`[^`]+`$/.test(part)) return `<code>${escapeHtml(part.slice(1, -1))}</code>`;
      let s = escapeHtml(part);
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>");
      s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
      s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
      return s;
    }).join("");
  }

  function codeBlock(code, lang) {
    const cls = lang ? ` class="language-${escapeHtml(lang.toLowerCase())}"` : "";
    return `<div class="code-block"><div class="code-head"><span>${escapeHtml(lang || "code")}</span>`
      + `<button type="button" class="code-copy" aria-label="Copy code">${ICONS.copy}<span>Copy</span></button></div>`
      + `<pre><code${cls}>${escapeHtml(code)}</code></pre></div>`;
  }

  const splitRow = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const TABLE_RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

  /** Small, safe Markdown renderer for answers (all text is HTML-escaped). */
  function renderMarkdown(src) {
    const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
    let html = "";
    let para = [];
    let list = null;
    const flushPara = () => {
      if (para.length) html += `<p>${inline(para.join(" "))}</p>`;
      para = [];
    };
    const flushList = () => {
      if (list) {
        const start = list.type === "ol" && list.start !== 1 ? ` start="${list.start}"` : "";
        html += `<${list.type}${start}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.type}>`;
      }
      list = null;
    };
    const flush = () => { flushPara(); flushList(); };

    for (let i = 0; i < lines.length; i++) {
      const raw = lines[i];
      const line = raw.trim();
      let m;

      // Fenced code: keep the text exactly; an unclosed fence (still streaming) runs to the end.
      if ((m = raw.match(/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)/))) {
        flush();
        const code = [];
        while (i + 1 < lines.length && !lines[i + 1].trim().startsWith(m[1])) code.push(lines[++i]);
        i++;
        html += codeBlock(code.join("\n"), m[2]);
        continue;
      }
      if (line.startsWith("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
        flush();
        const head = splitRow(line);
        const rows = [];
        i++;
        while (i + 1 < lines.length && lines[i + 1].trim().startsWith("|")) rows.push(splitRow(lines[++i]));
        html += `<div class="table-scroll"><table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead>`
          + `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
        continue;
      }
      if (!line) { flush(); continue; }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { flush(); const h = m[1].length <= 2 ? "h3" : "h4"; html += `<${h}>${inline(m[2])}</${h}>`; continue; }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) { flush(); html += "<hr>"; continue; }
      if (line.startsWith(">")) { flush(); html += `<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`; continue; }
      if ((m = line.match(/^[-*•+]\s+(.*)$/)) || (m = line.match(/^(\d+)[.)]\s+(.*)$/))) {
        const type = m.length === 3 ? "ol" : "ul";
        flushPara();
        // Keep the model's numbering when bullet points interrupt a numbered list.
        if (!list || list.type !== type) { flushList(); list = { type, items: [], start: type === "ol" ? Number(m[1]) : 1 }; }
        list.items.push(type === "ol" ? m[2] : m[1]);
        continue;
      }
      flushList();
      para.push(line);
    }
    flush();
    return html;
  }

  /** Syntax-highlight code blocks inside `root` with the bundled highlight.js. */
  function highlight(root) {
    if (!window.hljs) return;
    root.querySelectorAll("pre code:not([data-highlighted])").forEach((code) => {
      const lang = (code.className.match(/language-([\w+#.-]+)/) || [])[1];
      if (lang && !hljs.getLanguage(lang)) code.className = "";
      if (!lang && code.textContent.length > 20000) return;
      hljs.highlightElement(code);
    });
  }

  function md(text, extraClass) {
    const node = el("div", { class: `md ${extraClass || ""}`, html: renderMarkdown(text) });
    highlight(node);
    return node;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      // Not a secure context (e.g. opened via a LAN IP): fall back.
      const area = el("textarea", { style: "position:fixed;opacity:0" });
      area.value = text;
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
  }

  // Copy buttons on code blocks, wherever they are rendered.
  document.addEventListener("click", async (e) => {
    const button = e.target.closest(".code-copy");
    if (!button) return;
    await copyText(button.closest(".code-block").querySelector("code").textContent);
    button.lastElementChild.textContent = "Copied";
    setTimeout(() => { button.lastElementChild.textContent = "Copy"; }, 1500);
  });

  if (window.hljs) hljs.configure({ ignoreUnescapedHTML: true });

  const fmtDateTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "–");

  async function logout() {
    try { await request("POST", "/api/auth/logout"); } catch (_) { /* already signed out */ }
    location.href = "/";
  }

  return {
    ICONS, el, icon, api, request, errorText, escapeHtml, renderMarkdown, highlight, md, copyText,
    fmtDateTime, logout,
    setUnauthorizedHandler: (fn) => { onUnauthorized = fn; },
  };
})();
