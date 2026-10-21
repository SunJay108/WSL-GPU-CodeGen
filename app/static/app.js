"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const { ICONS, el, icon, api, request, renderMarkdown, highlight, md, copyText, logout } = window.UI;

  const EXAMPLES = [
    ["Validate an email", "Write a Python function that validates an email address with a regex, plus unit tests."],
    ["Threads vs processes", "Explain the difference between a process and a thread, with a Python example of each."],
    ["Find the bug", "Find the bug:\n\nfor i in range(len(items)):\n    if items[i] < 0:\n        items.remove(items[i])"],
    ["Dockerize FastAPI", "Write a production-ready Dockerfile for a FastAPI app served by uvicorn."],
  ];

  const state = {
    user: null,
    conversations: [],
    currentId: null,
    conversation: null,   // {id, title, messages, pending}
    sending: false,
    streamingId: null,    // conversation answered through this tab's open stream
    pollTimer: null,
    searchTimer: null,
    statusTimer: null,
    status: null,
    authMode: "login",
    authState: null,
  };

  function groupLabel(ts) {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
    if (ts >= today) return "Today";
    if (ts >= today - 86400) return "Yesterday";
    if (ts >= today - 7 * 86400) return "Previous 7 days";
    if (ts >= today - 30 * 86400) return "Previous 30 days";
    return new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "long" });
  }

  // ------------------------------------------------------------ sign in / sign up

  function showAuth(authState) {
    state.authState = authState || state.authState || {};
    clearTimeout(state.pollTimer);
    clearInterval(state.statusTimer);
    $("app").hidden = true;
    $("auth").hidden = false;
    $("tab-signup").hidden = !state.authState.allow_signup;
    setAuthMode("login");
    $("auth-username").focus();
  }

  function setAuthMode(mode) {
    state.authMode = mode;
    const signup = mode === "signup";
    $("tab-login").setAttribute("aria-selected", String(!signup));
    $("tab-signup").setAttribute("aria-selected", String(signup));
    $("auth-confirm-wrap").hidden = !signup;
    $("auth-confirm").required = signup;
    $("auth-password").autocomplete = signup ? "new-password" : "current-password";
    $("auth-submit").textContent = signup ? "Create account" : "Sign in";
    $("auth-hint").textContent = signup
      ? "Username: 3–32 letters, digits, dots, dashes or underscores. Password: at least 8 characters."
      : (state.authState.allow_signup ? "" : "Ask an administrator if you need an account.");
    $("auth-error").hidden = true;
  }

  async function submitAuth(e) {
    e.preventDefault();
    const signup = state.authMode === "signup";
    const username = $("auth-username").value.trim();
    const password = $("auth-password").value;
    const showError = (text) => { $("auth-error").textContent = text; $("auth-error").hidden = false; };
    if (signup && password !== $("auth-confirm").value) return showError("The passwords do not match.");

    $("auth-submit").disabled = true;
    try {
      const res = await api("POST", signup ? "/api/auth/signup" : "/api/auth/login", { username, password });
      $("auth-password").value = "";
      $("auth-confirm").value = "";
      startApp(res.user);
    } catch (err) {
      showError(err.message);
    } finally {
      $("auth-submit").disabled = false;
    }
  }

  function startApp(user) {
    state.user = user;
    $("auth").hidden = true;
    $("app").hidden = false;
    $("user-name").textContent = user.username;
    $("user-avatar").textContent = user.username.slice(0, 1).toUpperCase();
    const admin = user.role === "admin";
    $("user-role").hidden = !admin;
    $("admin-link").hidden = !admin;
    loadStatus();
    clearInterval(state.statusTimer);
    state.statusTimer = setInterval(loadStatus, 30000);
    loadConversations();
    showRoute();
  }

  // ------------------------------------------------------------ sidebar

  async function loadConversations() {
    const q = $("search").value.trim();
    try {
      state.conversations = await api("GET", `/api/conversations${q ? `?q=${encodeURIComponent(q)}` : ""}`);
    } catch (_) {
      return;
    }
    renderConversations();
  }

  function renderConversations() {
    const list = $("conv-list");
    list.replaceChildren();
    // The first question names a new chat on the server; show that title here too.
    const current = state.conversation && state.conversations.find((c) => c.id === state.conversation.id);
    if (current && current.title !== state.conversation.title) {
      state.conversation.title = current.title;
      $("chat-title").textContent = current.title;
    }
    if (!state.conversations.length) {
      list.append(el("div", { class: "conv-empty" }, $("search").value.trim() ? "No matching chats." : "No chats yet."));
      return;
    }
    let lastGroup = null;
    for (const conv of state.conversations) {
      const group = groupLabel(conv.updated_at);
      if (group !== lastGroup) {
        list.append(el("div", { class: "conv-group" }, group));
        lastGroup = group;
      }
      const item = el("a", {
        class: `conv-item${conv.id === state.currentId ? " active" : ""}`,
        href: `#/c/${conv.id}`,
        title: conv.title,
      },
        el("span", { class: "conv-title" }, conv.title),
        el("span", { class: "conv-actions" },
          el("button", { type: "button", title: "Rename", "aria-label": "Rename", onclick: (e) => { e.preventDefault(); e.stopPropagation(); startRename(item, conv); } }, icon("edit")),
          el("button", { type: "button", title: "Delete", "aria-label": "Delete", onclick: (e) => { e.preventDefault(); e.stopPropagation(); deleteConversation(conv); } }, icon("trash"))));
      item.addEventListener("click", () => closeSidebar());
      list.append(item);
    }
  }

  function startRename(item, conv) {
    const input = el("input", { class: "conv-rename", value: conv.title, maxlength: "200" });
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const title = input.value.trim();
      if (save && title && title !== conv.title) {
        try { await api("PATCH", `/api/conversations/${conv.id}`, { title }); } catch (err) { alert(err.message); }
        if (state.conversation && state.conversation.id === conv.id) {
          state.conversation.title = title;
          $("chat-title").textContent = title;
        }
      }
      loadConversations();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); });
    item.replaceChildren(input);
    input.focus();
    input.select();
  }

  async function deleteConversation(conv) {
    if (!confirm(`Delete the chat "${conv.title}"?`)) return;
    try {
      await api("DELETE", `/api/conversations/${conv.id}`);
    } catch (err) {
      alert(err.message);
      return;
    }
    if (conv.id === state.currentId) location.hash = "";
    loadConversations();
  }

  function openSidebar() { $("app").classList.add("sidebar-open"); $("scrim").hidden = false; }
  function closeSidebar() { $("app").classList.remove("sidebar-open"); $("scrim").hidden = true; }

  // ------------------------------------------------------------ conversation view

  function routeFromHash() {
    const m = location.hash.match(/^#\/c\/([0-9a-f-]+)$/);
    return m ? m[1] : null;
  }

  async function showRoute() {
    if (!state.user) return;
    clearTimeout(state.pollTimer);
    const id = routeFromHash();
    state.currentId = id;
    if (!id) {
      state.conversation = null;
      renderThread();
      renderConversations();
      $("input").focus();
      return;
    }
    try {
      state.conversation = await api("GET", `/api/conversations/${id}`);
    } catch (_) {
      location.hash = "";
      return;
    }
    renderThread();
    renderConversations();
    scrollToBottom(true);
    schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    const conv = state.conversation;
    if (!conv || !conv.pending || state.streamingId === conv.id) return;
    // Answer running from another tab or before a reload: follow it.
    state.pollTimer = setTimeout(async () => {
      if (!state.conversation || state.conversation.id !== conv.id) return;
      try {
        state.conversation = await api("GET", `/api/conversations/${conv.id}`);
      } catch (_) { return; }
      if (state.conversation.pending) updatePending(); else { renderThread(); scrollToBottom(); }
      schedulePoll();
    }, 1200);
  }

  function renderThread() {
    const conv = state.conversation;
    const box = $("messages");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    box.replaceChildren();
    $("chat-title").textContent = conv ? conv.title : "New chat";
    $("export-btn").hidden = !(conv && conv.messages.length);

    if (!conv || (!conv.messages.length && !conv.pending)) {
      box.append(renderWelcome());
      updateComposer();
      return;
    }
    const thread = el("div", { class: "thread" });
    for (const msg of conv.messages) thread.append(msg.role === "user" ? renderUser(msg) : renderAssistant(msg));
    if (conv.pending) thread.append(renderPending(conv.pending));
    box.append(thread);
    if (atBottom) scrollToBottom(true);
    updateComposer();
  }

  function renderWelcome() {
    return el("div", { class: "welcome" },
      el("h2", {}, "What are we building today?"),
      el("p", {}, "Ask about code, bugs, architecture, tests or tooling. Every question and answer is checked by Qwen3Guard."),
      el("div", { class: "examples" },
        EXAMPLES.map(([title, q]) => el("button", { class: "example", type: "button", onclick: () => ask(q) },
          el("strong", {}, title), el("span", {}, q.replace(/\s+/g, " "))))));
  }

  const renderUser = (msg) => el("div", { class: "msg-user" }, msg.content);

  function renderAssistant(msg) {
    const data = msg.data || {};
    const body = el("div", { class: "msg-body" });

    if (data.status && data.status !== "answered") {
      body.append(el("div", { class: `notice ${data.status}` }, md(data.message || msg.content)));
    } else {
      body.append(md(msg.content));
    }

    const copyBtn = el("button", { class: "icon-btn", type: "button", title: "Copy answer", "aria-label": "Copy answer" }, icon("copy"));
    copyBtn.addEventListener("click", async () => {
      await copyText(msg.content);
      copyBtn.replaceChildren(icon("check"));
      setTimeout(() => copyBtn.replaceChildren(icon("copy")), 1500);
    });
    const answered = !data.status || data.status === "answered";
    body.append(el("div", { class: "msg-actions" }, copyBtn,
      data.stopped ? el("span", { class: "stopped-tag" }, "Stopped") : null,
      answered && data.seconds ? `Answered in ${data.seconds} s` : null));

    return el("div", { class: "msg-assistant" }, el("div", { class: "avatar", html: ICONS.logo }), body);
  }

  function renderPending(p) {
    const partial = el("div", { class: "md streaming", id: "pending-partial", hidden: !p.partial, html: renderMarkdown(p.partial || "") });
    highlight(partial);
    const body = el("div", { class: "msg-body", id: "pending" },
      el("div", { class: "pending-stage" }, el("span", { class: "spinner" }), el("span", { id: "pending-text" }, p.text || "Working…")),
      partial);
    return el("div", { class: "msg-assistant" }, el("div", { class: "avatar", html: ICONS.logo }), body);
  }

  function updatePending() {
    const p = state.conversation && state.conversation.pending;
    if (!p) return;
    if (!$("pending")) { renderThread(); return; }
    const box = $("messages");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    $("pending-text").textContent = p.text || "Working…";
    const partial = $("pending-partial");
    partial.hidden = !p.partial;
    if (p.partial) {
      partial.innerHTML = renderMarkdown(p.partial);
      highlight(partial);
    }
    if (atBottom) scrollToBottom(true);
  }

  function scrollToBottom(instant) {
    const box = $("messages");
    box.scrollTo({ top: box.scrollHeight, behavior: instant ? "auto" : "smooth" });
  }

  // ------------------------------------------------------------ asking

  async function ask(text) {
    text = (text || "").trim();
    if (!text || state.sending) return;
    if (state.conversation && state.conversation.pending) return;

    state.sending = true;
    $("input").value = "";
    autosize();
    let id = state.currentId;

    try {
      if (!id) {
        const conv = await api("POST", "/api/conversations");
        id = conv.id;
        state.currentId = id;
        state.conversation = { ...conv, messages: [], pending: null };
        history.replaceState(null, "", `#/c/${id}`);
      }
      const conv = state.conversation;
      conv.messages.push({ id: "pending-user", role: "user", content: text });
      conv.pending = { stage: "queued", text: "Starting…", partial: "" };
      state.streamingId = id;
      renderThread();
      scrollToBottom(true);

      const res = await request("POST", `/api/conversations/${id}/ask`, { question: text });
      await readEvents(res, (event, data) => onEvent(id, event, data));
    } catch (err) {
      if (state.currentId === id && state.conversation) {
        const conv = state.conversation;
        conv.pending = null;
        conv.messages = conv.messages.filter((m) => m.id !== "pending-user");
        const msg = `⚠️ ${err.message}`;
        conv.messages.push({ id: `err${Date.now()}`, role: "assistant", content: msg, data: { status: "error", message: msg } });
        renderThread();
      }
    } finally {
      state.sending = false;
      state.streamingId = null;
      updateComposer();
      loadConversations();
      if (state.currentId === id) schedulePoll();
    }
  }

  function onEvent(id, event, data) {
    const conv = state.conversation;
    if (!conv || conv.id !== id) return;  // user switched to another chat meanwhile
    if (event === "user") {
      const tmp = conv.messages.find((m) => m.id === "pending-user");
      if (tmp) Object.assign(tmp, data.message);
      loadConversations();
    } else if (event === "stage") {
      conv.pending = { ...(conv.pending || {}), stage: data.stage, text: data.text };
      updatePending();
    } else if (event === "delta") {
      conv.pending = { ...(conv.pending || {}), partial: data.text };
      updatePending();
    } else if (event === "done" || event === "error") {
      conv.pending = null;
      if (!conv.messages.some((m) => m.id === data.message.id)) conv.messages.push(data.message);
      renderThread();
    }
  }

  /** Read a Server-Sent Events response (EventSource cannot POST). */
  async function readEvents(res, onEvent) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        let event = "message";
        let data = "";
        for (const line of chunk.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (data) onEvent(event, JSON.parse(data));
      }
    }
  }

  async function stopAnswer() {
    if (!state.currentId) return;
    $("stop").disabled = true;
    try { await api("POST", `/api/conversations/${state.currentId}/stop`); } catch (_) { /* finishing anyway */ }
  }

  function updateComposer() {
    const busy = state.sending || !!(state.conversation && state.conversation.pending);
    $("send").hidden = busy;
    $("stop").hidden = !busy;
    if (!busy) $("stop").disabled = false;
    $("send").disabled = !$("input").value.trim();
    $("input").placeholder = busy ? "Waiting for the answer…" : "Ask a programming question…";
    document.querySelectorAll(".example").forEach((b) => { b.disabled = busy; });
  }

  function autosize() {
    const input = $("input");
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
    updateComposer();
  }

  // ------------------------------------------------------------ status / account

  async function loadStatus() {
    try {
      state.status = await api("GET", "/api/status");
    } catch (_) {
      $("model-info").replaceChildren(el("span", { class: "dot down" }), "CodeGen server unreachable");
      return;
    }
    const s = state.status;
    if (s.max_question_chars) $("input").maxLength = s.max_question_chars;
    const problem = !s.services.llm ? "Model server unreachable" : !s.services.guard ? "Safety filter unavailable" : null;
    $("model-info").replaceChildren(el("span", { class: `dot${problem ? " down" : ""}` }),
      problem || `${s.model} · filter on`);
  }

  function openPasswordDialog() {
    $("password-form").reset();
    $("pw-error").hidden = true;
    $("password-dialog").showModal();
  }

  async function savePassword(e) {
    e.preventDefault();
    const showError = (text) => { $("pw-error").textContent = text; $("pw-error").hidden = false; };
    if ($("pw-new").value !== $("pw-repeat").value) return showError("The new passwords do not match.");
    try {
      await api("POST", "/api/auth/password", { current: $("pw-current").value, new: $("pw-new").value });
    } catch (err) {
      return showError(err.message);
    }
    $("password-dialog").close();
    alert("Password changed. Your other sessions were signed out.");
  }

  // ------------------------------------------------------------ wiring

  UI.setUnauthorizedHandler(async () => {
    state.user = null;
    try { showAuth(await api("GET", "/api/auth/state")); } catch (_) { showAuth(); }
  });

  $("auth-form").addEventListener("submit", submitAuth);
  $("tab-login").addEventListener("click", () => setAuthMode("login"));
  $("tab-signup").addEventListener("click", () => setAuthMode("signup"));
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); ask($("input").value); });
  $("stop").addEventListener("click", stopAnswer);
  $("input").addEventListener("input", autosize);
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      ask($("input").value);
    }
  });
  $("new-chat").addEventListener("click", () => { location.hash = ""; closeSidebar(); $("input").focus(); });
  $("search").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(loadConversations, 250);
  });
  $("clear-all").addEventListener("click", async () => {
    if (!confirm("Delete all your chats? You will no longer see them.")) return;
    try { await api("DELETE", "/api/conversations"); } catch (err) { alert(err.message); return; }
    location.hash = "";
    loadConversations();
  });
  $("export-btn").addEventListener("click", () => {
    if (state.currentId) location.href = `/api/conversations/${state.currentId}/export`;
  });
  $("password-btn").addEventListener("click", openPasswordDialog);
  $("password-form").addEventListener("submit", savePassword);
  $("pw-cancel").addEventListener("click", () => $("password-dialog").close());
  $("logout-btn").addEventListener("click", logout);
  $("open-sidebar").addEventListener("click", openSidebar);
  $("close-sidebar").addEventListener("click", closeSidebar);
  $("scrim").addEventListener("click", closeSidebar);
  window.addEventListener("hashchange", showRoute);

  (async () => {
    let authState;
    try {
      authState = await api("GET", "/api/auth/state");
    } catch (err) {
      document.body.textContent = `CodeGen server unreachable: ${err.message}`;
      return;
    }
    if (authState.user) startApp(authState.user); else showAuth(authState);
  })();
})();
