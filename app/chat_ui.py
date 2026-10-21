import html
import logging
import os
import re
import threading
import time
import uuid

import gradio as gr

import auth
import db
import filter
import llm

log = logging.getLogger("chat")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSS_PATH = os.path.join(BASE_DIR, "app.css")
LOGO_PATH = os.path.join(BASE_DIR, "static", "logo.png")

# Text is released to the screen only after Qwen3Guard-Stream has checked it,
# in batches of roughly this many characters.
FEED_CHARS = 48

# Per browser session, the generation allowed to update the screen. Opening another
# chat replaces it, so a running generation stops and drops its answer silently.
ACTIVE: dict = {}
# Generations whose user pressed Stop: they end early but keep the partial answer.
STOPPED: set = set()

THEME = gr.themes.Base(
    primary_hue="emerald",
    neutral_hue="neutral",
    radius_size=gr.themes.sizes.radius_lg,
    # System fonts only: the app must not reach out to Google Fonts when offline.
    font=["ui-sans-serif", "-apple-system", "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif"],
    font_mono=["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "Liberation Mono", "monospace"],
).set(
    body_background_fill="#ffffff",
    body_background_fill_dark="#212121",
    body_text_color="#0d0d0d",
    body_text_color_dark="#ececec",
    body_text_color_subdued="#6b6b6b",
    body_text_color_subdued_dark="#a3a3a3",
)

# On small screens the sidebar is an overlay: close it when the user taps outside
# it, or picks a chat / starts a new one.
MOBILE_SIDEBAR_JS = """
() => {
  document.addEventListener("click", (event) => {
    const sidebar = document.querySelector("#sidebar");
    if (!sidebar || window.innerWidth > 768 || !sidebar.classList.contains("cg-collapsed")) return;
    const picked = event.target.closest("#chat-list label, #new-chat");
    const outside = !sidebar.contains(event.target) && !event.target.closest("#toggle-sidebar");
    if (picked || outside) setTimeout(() => sidebar.classList.remove("cg-collapsed"), 150);
  });
}
"""

with open(CSS_PATH, encoding="utf-8") as f:
    CSS = f.read()

EMPTY_STATE = """
<div class="cg-empty">
  <img src="/static/logo.png" alt="">
  <h2>What are we building today?</h2>
  <p>Ask about code, bugs, architecture, tests or tooling.</p>
</div>
"""

SUGGESTIONS = [
    ("Validate an email", "Write a Python function that validates an email address with a regex, plus unit tests."),
    ("Threads vs processes", "Explain the difference between a process and a thread, with a Python example of each."),
    ("Find the bug", "Find the bug:\n\nfor i in range(len(items)):\n    if items[i] < 0:\n        items.remove(items[i])"),
    ("Dockerize FastAPI", "Write a production-ready Dockerfile for a FastAPI app served by uvicorn."),
]

# =========================================
# Helpers
# =========================================

def fix_markdown(text: str) -> str:
    """Close an unfinished code fence so partial output renders correctly."""
    if text.count("```") % 2 != 0:
        text += "\n```"
    return text


def status_html(label: str) -> str:
    return f'<span class="cg-status"><span class="cg-pulse"></span>{html.escape(label)}</span>'


def make_title(query: str) -> str:
    title = re.sub(r"\s+", " ", query).strip()
    return (title[:57] + "...") if len(title) > 60 else (title or "New chat")


def to_chatbot(turns):
    return [[t["q"], t["a"]] for t in turns]


def saved_turns(turns):
    """Exchanges the model may see as context (blocked ones are excluded)."""
    return [(t["q"], t["a"]) for t in turns if t["ok"]]


def chat_choices(user_id: int):
    return [(row["title"] or "Untitled", row["id"]) for row in db.list_chats(user_id)]


def title_html(text: str) -> str:
    return f'<div class="cg-title">{html.escape(text)}</div>'


def user_card(user: dict) -> str:
    initial = html.escape(user["username"][:1].upper())
    name = html.escape(user["username"])
    role = "Administrator" if user["role"] == "admin" else "User"
    admin_link = (
        '<a class="cg-menu-link" href="/admin/"><span class="cg-ico" style="--icon:url(/static/icons/shield.svg)"></span>Admin panel</a>'
        if user["role"] == "admin" else ""
    )
    return f"""
<div class="cg-user">
  <div class="cg-avatar">{initial}</div>
  <div class="cg-user-meta"><div class="cg-user-name">{name}</div><div class="cg-user-role">{role}</div></div>
</div>
<nav class="cg-menu">{admin_link}</nav>
"""


LOGOUT_LINK = (
    '<nav class="cg-menu"><a class="cg-menu-link" href="/logout">'
    '<span class="cg-ico" style="--icon:url(/static/icons/logout.svg)"></span>Log out</a></nav>'
)

# =========================================
# Event handlers
# =========================================

def on_load(request: gr.Request):
    user = auth.require_user(request)
    return user_card(user), gr.update(choices=chat_choices(user["id"]), value=None)


def supersede(request: gr.Request):
    """Detach any running generation of this browser session from the screen."""
    ACTIVE[request.session_hash] = uuid.uuid4().hex


def new_chat(request: gr.Request):
    supersede(request)
    user = auth.require_user(request)
    return (
        [], [], None,
        gr.update(choices=chat_choices(user["id"]), value=None),
        title_html("New chat"),
        gr.update(visible=False),
        gr.update(visible=True),
        gr.update(visible=True), gr.update(visible=False),
    )


def open_chat(chat_id, request: gr.Request):
    supersede(request)
    user = auth.require_user(request)
    chat = db.get_chat(chat_id) if chat_id else None
    if chat is None or chat["user_id"] != user["id"]:
        raise gr.Error("Chat not found.")

    turns = [{"q": q, "a": a, "ok": True} for q, a in db.chat_turns(chat_id)]
    return (
        to_chatbot(turns), turns, chat_id,
        title_html(chat["title"] or "Untitled"),
        gr.update(visible=True),
        gr.update(visible=False),
        gr.update(visible=True), gr.update(visible=False),
    )


def start_turn(text):
    """Clear the input box immediately and swap Send for Stop."""
    return "", text, gr.update(visible=False), gr.update(visible=True), gr.update(visible=False)


def after_turn(chat_id, request: gr.Request):
    """Refresh the title, chat list and buttons once a generation has ended."""
    user = auth.require_user(request)
    chat = db.get_chat(chat_id) if chat_id else None
    if chat is not None and chat["user_id"] != user["id"]:
        chat = None
    return (
        title_html(chat["title"] if chat else "New chat"),
        gr.update(visible=chat is not None),
        gr.update(choices=chat_choices(user["id"]), value=chat["id"] if chat else None),
        gr.update(visible=True),
        gr.update(visible=False),
    )


def request_stop(request: gr.Request):
    STOPPED.add(ACTIVE.get(request.session_hash))


def respond(query, turns, chat_id, request: gr.Request):
    """
    Outputs: chatbot, turns, chat_id.
    Streams the answer, but only text that Qwen3Guard-Stream has already cleared.
    """
    user = auth.require_user(request)
    key, generation = request.session_hash, uuid.uuid4().hex
    ACTIVE[key] = generation
    current = lambda: ACTIVE.get(key) == generation
    should_stop = lambda: not current() or generation in STOPPED

    query = filter.sanitize(query or "")
    if not query:
        return

    shown_turns = to_chatbot(turns)
    yield shown_turns + [[query, status_html("Checking your request")]], turns, chat_id

    context = saved_turns(turns)
    verdict, monitor = filter.screen_prompt(query, context[-1][1] if context else "")

    messages = None
    if verdict.allowed:
        try:
            messages, max_tokens = llm.build_messages(context, query)
        except llm.PromptTooLong as error:
            verdict = filter.Verdict(False, "prompt", "input", "TOO_LONG", str(error), f"📏 **{error}**")

    answer, error_text = "", None
    try:
        if verdict.allowed and current():
            yield shown_turns + [[query, status_html("Thinking")]], turns, chat_id

            pending = ""
            for chunk in llm.stream_chat(messages, max_tokens, should_stop):
                pending += chunk
                if len(pending) < FEED_CHARS:
                    continue
                verdict = monitor.feed(pending)
                if not verdict.allowed:
                    break
                answer += pending
                pending = ""
                if current():
                    yield shown_turns + [[query, fix_markdown(answer)]], turns, chat_id

            if verdict.allowed and current():
                verdict = monitor.feed(pending, final=True)
            if verdict.allowed and current():
                answer += pending
                yield shown_turns + [[query, fix_markdown(answer) + "\n\n" + status_html("Reviewing answer")]], \
                    turns, chat_id
                verdict = monitor.finish(query, answer)
    except Exception as error:
        log.exception("Generation failed")
        error_text = f"❌ **The model server returned an error:** {html.escape(str(error))[:300]}"
    finally:
        if monitor is not None:
            monitor.close()
        stopped = generation in STOPPED
        STOPPED.discard(generation)

    if not verdict.allowed:
        db.log_moderation(user["id"], chat_id, verdict.stage, verdict.checker, verdict.verdict,
                          verdict.detail, query if verdict.stage == "prompt" else f"Q: {query}\n\nA: {answer}")
    # The user moved to another chat meanwhile: nothing to show or save. Gradio
    # re-sends a generator's last output when it ends, so finish with a no-op
    # instead of letting the stale answer repaint the newly opened chat.
    if not current():
        yield gr.update(), gr.update(), gr.update()
        return

    if error_text or not verdict.allowed:
        turns = turns + [{"q": query, "a": error_text or verdict.message, "ok": False}]
        yield to_chatbot(turns), turns, chat_id
        return

    answer = fix_markdown(answer.strip()) or "_(no response)_"
    if stopped:
        answer += "\n\n*⏹ Stopped.*"

    if not chat_id:
        chat_id = db.create_chat(user["id"], make_title(query))
    db.add_exchange(chat_id, query, answer)

    turns = turns + [{"q": query, "a": answer, "ok": True}]
    yield to_chatbot(turns), turns, chat_id


def confirm_delete(chat_id, request: gr.Request):
    user = auth.require_user(request)
    chat = db.get_chat(chat_id) if chat_id else None
    if chat is not None and chat["user_id"] == user["id"]:
        db.delete_chat(chat_id)
    return (*new_chat(request), gr.update(visible=False))


def save_password(current, new, confirm, request: gr.Request):
    if new != confirm:
        return gr.update(value="⚠️ New passwords do not match."), gr.update(), gr.update(), gr.update(), gr.update()
    error = auth.change_password(request, current, new)
    if error:
        return gr.update(value=f"⚠️ {error}"), gr.update(), gr.update(), gr.update(), gr.update()
    gr.Info("Password changed. Other sessions were signed out.")
    return gr.update(value=""), "", "", "", gr.update(visible=False)


def warm_up():
    """Prime the classifier slot so the first real request is not slow."""
    def run():
        for _ in range(30):
            try:
                filter.topic_check("prompt", filter.PROMPT_TOPIC_SYSTEM, filter.PROMPT_TOPIC_EXAMPLES,
                                   filter.topic_input("How do I reverse a list in Python?"), filter.PROMPT_LABELS)
                log.info("Topic classifier warmed up")
                return
            except Exception:
                time.sleep(5)
        log.warning("Topic classifier warm-up gave up; the first request may be slow")

    threading.Thread(target=run, daemon=True).start()

# =========================================
# Layout
# =========================================

with gr.Blocks(title="CodeGen", css=CSS, theme=THEME, js=MOBILE_SIDEBAR_JS, analytics_enabled=False,
               fill_height=True) as chat_app:
    turns_state = gr.State([])
    chat_id_state = gr.State(None)
    pending_query = gr.State("")

    with gr.Row(elem_id="cg-shell", equal_height=False):

        # ---------------------
        # Sidebar
        # ---------------------
        with gr.Column(elem_id="sidebar", scale=0, min_width=260):
            gr.HTML('<div class="cg-brand"><img src="/static/logo.png" alt=""><span>CodeGen</span></div>')
            new_btn = gr.Button("New chat", elem_id="new-chat", elem_classes="cg-icon-btn cg-side-btn")
            gr.HTML('<div class="cg-section">Chats</div>')
            chat_list = gr.Radio(choices=[], value=None, show_label=False, container=False, elem_id="chat-list")
            with gr.Column(elem_id="user-panel"):
                user_html = gr.HTML()
                pwd_btn = gr.Button("Change password", elem_id="pwd-btn", elem_classes="cg-icon-btn cg-side-btn")
                gr.HTML(LOGOUT_LINK)

        # ---------------------
        # Main panel
        # ---------------------
        with gr.Column(elem_id="main", scale=1):
            with gr.Row(elem_id="topbar"):
                toggle_btn = gr.Button("", elem_id="toggle-sidebar", elem_classes="cg-icon-btn", scale=0, min_width=40)
                chat_title = gr.HTML(title_html("New chat"))
                delete_btn = gr.Button("", elem_id="delete-chat", elem_classes="cg-icon-btn", scale=0,
                                       min_width=40, visible=False)

            chatbot = gr.Chatbot(
                elem_id="chatbot",
                show_label=False,
                container=False,
                placeholder=EMPTY_STATE,
                avatar_images=(None, LOGO_PATH),
                show_copy_button=True,
                bubble_full_width=False,
                latex_delimiters=[],
                height="100%",
            )

            with gr.Row(elem_id="suggestions") as suggestions:
                suggestion_btns = [gr.Button(label, elem_classes="cg-suggestion") for label, _ in SUGGESTIONS]

            with gr.Row(elem_id="composer"):
                prompt = gr.Textbox(
                    placeholder="Message CodeGen",
                    lines=1,
                    max_lines=12,
                    show_label=False,
                    container=False,
                    autofocus=True,
                    elem_id="prompt",
                    scale=1,
                )
                send_btn = gr.Button("", elem_id="send-btn", elem_classes="cg-icon-btn", scale=0, min_width=40)
                stop_btn = gr.Button("", elem_id="stop-btn", elem_classes="cg-icon-btn", scale=0, min_width=40,
                                     visible=False)

            gr.HTML('<div id="disclaimer">CodeGen answers programming questions only. '
                    'Every message is checked by Qwen3Guard.</div>')

    # ---------------------
    # Modals
    # ---------------------
    with gr.Column(visible=False, elem_classes="cg-modal") as delete_modal:
        with gr.Column(elem_classes="cg-modal-card"):
            gr.HTML("<h3>Delete chat?</h3><p>This permanently deletes the conversation.</p>")
            with gr.Row():
                cancel_delete_btn = gr.Button("Cancel", elem_classes="cg-btn-secondary")
                confirm_delete_btn = gr.Button("Delete", variant="stop")

    with gr.Column(visible=False, elem_classes="cg-modal") as password_modal:
        with gr.Column(elem_classes="cg-modal-card"):
            gr.HTML("<h3>Change password</h3>")
            current_pwd = gr.Textbox(label="Current password", type="password")
            new_pwd = gr.Textbox(label="New password", type="password")
            confirm_pwd = gr.Textbox(label="Confirm new password", type="password")
            pwd_status = gr.Markdown()
            with gr.Row():
                cancel_pwd_btn = gr.Button("Cancel", elem_classes="cg-btn-secondary")
                save_pwd_btn = gr.Button("Save", variant="primary")

    # ---------------------
    # Events
    # ---------------------
    chat_app.load(on_load, outputs=[user_html, chat_list])

    # Only the chat area is an output while streaming; Gradio marks every output of a
    # running event as pending, so the title, list and buttons are refreshed afterwards.
    for trigger in (prompt.submit, send_btn.click):
        trigger(
            start_turn, [prompt], [prompt, pending_query, send_btn, stop_btn, suggestions], queue=False,
        ).then(
            respond,
            [pending_query, turns_state, chat_id_state],
            [chatbot, turns_state, chat_id_state],
            show_progress="hidden",
            concurrency_limit=4,
        ).then(
            after_turn, [chat_id_state], [chat_title, delete_btn, chat_list, send_btn, stop_btn],
        )

    stop_btn.click(request_stop, queue=False)

    reset_outputs = [chatbot, turns_state, chat_id_state, chat_list, chat_title, delete_btn, suggestions,
                     send_btn, stop_btn]
    new_btn.click(new_chat, outputs=reset_outputs)
    chat_list.input(
        open_chat, [chat_list],
        [chatbot, turns_state, chat_id_state, chat_title, delete_btn, suggestions, send_btn, stop_btn],
    )

    toggle_btn.click(None, js="() => document.querySelector('#sidebar').classList.toggle('cg-collapsed')")

    for button, (_, text) in zip(suggestion_btns, SUGGESTIONS):
        button.click(lambda t=text: t, outputs=prompt, queue=False)

    delete_btn.click(lambda: gr.update(visible=True), outputs=delete_modal, queue=False)
    cancel_delete_btn.click(lambda: gr.update(visible=False), outputs=delete_modal, queue=False)
    confirm_delete_btn.click(confirm_delete, [chat_id_state], reset_outputs + [delete_modal])

    pwd_btn.click(lambda: (gr.update(visible=True), ""), outputs=[password_modal, pwd_status], queue=False)
    cancel_pwd_btn.click(lambda: ("", "", "", gr.update(visible=False)),
                         outputs=[current_pwd, new_pwd, confirm_pwd, password_modal], queue=False)
    save_pwd_btn.click(save_password, [current_pwd, new_pwd, confirm_pwd],
                       [pwd_status, current_pwd, new_pwd, confirm_pwd, password_modal])
