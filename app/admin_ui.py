import html

import gradio as gr
import pandas as pd

import auth
import db
from chat_ui import CSS, THEME

USER_COLUMNS = ["ID", "Username", "Role", "Status", "Chats", "Created", "Last login"]
CHAT_COLUMNS = ["User", "Title", "Messages", "Updated"]
MODERATION_COLUMNS = ["Time", "User", "Stage", "Check", "Verdict", "Detail", "Content"]
ALL_USERS = "All users"

# =========================================
# Rendering helpers
# =========================================

def stats_html() -> str:
    s = db.stats()
    cards = [
        ("Users", f"{s['active_users']}<small> / {s['users']}</small>", "active / total"),
        ("Chats", s["chats"], "saved conversations"),
        ("Messages", s["messages"], "questions and answers"),
        ("Blocked", s["blocked_24h"], "by the filter in the last 24 h"),
    ]
    return '<div class="cg-stats">' + "".join(
        f'<div class="cg-stat"><div class="cg-stat-label">{label}</div>'
        f'<div class="cg-stat-value">{value}</div><div class="cg-stat-hint">{hint}</div></div>'
        for label, value, hint in cards
    ) + "</div>"


def users_frame() -> pd.DataFrame:
    rows = [
        [u["id"], u["username"], u["role"], "active" if u["is_active"] else "disabled",
         u["chat_count"], u["created_at"], u["last_login_at"] or "never"]
        for u in db.list_users()
    ]
    return pd.DataFrame(rows, columns=USER_COLUMNS)


def user_choices():
    return [
        (f"{u['username']} ({u['role']}{'' if u['is_active'] else ', disabled'})", u["id"])
        for u in db.list_users()
    ]


def refresh_users():
    choices = user_choices()
    filter_choices = [(ALL_USERS, 0)] + [(label.split(" (")[0], uid) for label, uid in choices]
    return (
        users_frame(),
        gr.update(choices=choices, value=None),
        gr.update(choices=filter_choices),
        gr.update(choices=filter_choices),
        stats_html(),
    )


def chats_frame(user_filter, search):
    rows = db.admin_list_chats(user_filter or None, (search or "").strip())
    frame = pd.DataFrame(
        [[r["username"], r["title"] or "Untitled", r["message_count"], r["updated_at"]] for r in rows],
        columns=CHAT_COLUMNS,
    )
    return frame, [r["id"] for r in rows]


def moderation_frame(user_filter) -> pd.DataFrame:
    rows = db.list_moderation(user_filter or None)
    return pd.DataFrame(
        [[r["created_at"], r["username"], r["stage"], r["checker"], r["verdict"], r["detail"] or "",
          (r["content"] or "")[:300]] for r in rows],
        columns=MODERATION_COLUMNS,
    )

# =========================================
# Handlers
# =========================================

def on_load(request: gr.Request):
    admin = auth.require_admin(request)
    header = f"""
<div class="cg-admin-header">
  <div class="cg-brand"><img src="/static/logo.png" alt=""><span>CodeGen Admin</span></div>
  <nav class="cg-admin-nav">
    <span class="cg-admin-who">Signed in as <b>{html.escape(admin['username'])}</b></span>
    <a href="/">← Back to chat</a>
    <a href="/logout">Log out</a>
  </nav>
</div>"""
    chats, chat_ids = chats_frame(0, "")
    return (header, *refresh_users(), chats, chat_ids, moderation_frame(0))


def create_user(username, password, role, request: gr.Request):
    auth.require_admin(request)
    username = (username or "").strip()
    error = auth.username_error(username) or auth.password_error(password)
    if error is None and db.get_user_by_name(username):
        error = f"User '{username}' already exists."
    if error:
        return (f"⚠️ {error}", gr.update(), gr.update(), *(gr.update(),) * 5)

    db.create_user(username, auth.hash_password(password), role)
    return (f"✅ Created {role} '{username}'.", "", "", *refresh_users())


def select_user(user_id, request: gr.Request):
    auth.require_admin(request)
    user = db.get_user(user_id) if user_id else None
    if user is None:
        return gr.update(value=None), gr.update(value=False), ""
    return gr.update(value=user["role"]), gr.update(value=bool(user["is_active"])), ""


def last_admin_guard(target, new_role, new_active) -> str:
    """Refuse changes that would leave no active administrator."""
    losing_admin = target["role"] == "admin" and target["is_active"] and (new_role != "admin" or not new_active)
    if losing_admin and db.count_active_admins() <= 1:
        return "This is the last active administrator; promote another admin first."
    return ""


def save_user(user_id, role, active, new_password, request: gr.Request):
    admin = auth.require_admin(request)
    target = db.get_user(user_id) if user_id else None
    if target is None:
        return ("⚠️ Select a user first.", gr.update(), *(gr.update(),) * 5)

    if target["id"] == admin["id"] and (role != "admin" or not active):
        return ("⚠️ You cannot demote or disable your own account.", gr.update(), *(gr.update(),) * 5)
    error = last_admin_guard(target, role, active)
    if not error and new_password:
        error = auth.password_error(new_password) or ""
    if error:
        return (f"⚠️ {error}", gr.update(), *(gr.update(),) * 5)

    db.update_user(
        target["id"], role=role, is_active=bool(active),
        password_hash=auth.hash_password(new_password) if new_password else None,
    )
    # A password reset or a disabled account signs the user out everywhere.
    if new_password or not active:
        db.delete_user_sessions(target["id"])

    changes = [f"role={role}", "active" if active else "disabled"] + (["password reset"] if new_password else [])
    return (f"✅ Updated '{target['username']}': {', '.join(changes)}.", "", *refresh_users())


def delete_user(user_id, confirmed, request: gr.Request):
    admin = auth.require_admin(request)
    target = db.get_user(user_id) if user_id else None
    if target is None:
        return ("⚠️ Select a user first.", gr.update(), *(gr.update(),) * 5)
    if not confirmed:
        return ("⚠️ Tick the confirmation box to delete this user.", gr.update(), *(gr.update(),) * 5)
    if target["id"] == admin["id"]:
        return ("⚠️ You cannot delete your own account.", gr.update(), *(gr.update(),) * 5)
    error = last_admin_guard(target, "user", False)
    if error:
        return (f"⚠️ {error}", gr.update(), *(gr.update(),) * 5)

    db.delete_user(target["id"])
    return (f"✅ Deleted '{target['username']}' and their chats.", False, *refresh_users())


def search_chats(user_filter, search, request: gr.Request):
    auth.require_admin(request)
    frame, chat_ids = chats_frame(user_filter, search)
    return frame, chat_ids, None, [], "Select a conversation to view it.", f"{len(chat_ids)} chat(s)."


def view_chat(chat_ids, evt: gr.SelectData, request: gr.Request):
    auth.require_admin(request)
    row = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
    if row is None or row >= len(chat_ids):
        return None, [], "Select a conversation to view it."
    chat = db.get_chat(chat_ids[row])
    if chat is None:
        return None, [], "This chat no longer exists."
    owner = db.get_user(chat["user_id"]) if chat["user_id"] else None
    meta = (
        f"**{html.escape(chat['title'] or 'Untitled')}**  \n"
        f"Owner: {html.escape(owner['username']) if owner else '(deleted)'}  \n"
        f"Created: {chat['created_at']}  \nUpdated: {chat['updated_at']}"
    )
    return chat["id"], [list(turn) for turn in db.chat_turns(chat["id"])], meta


def delete_selected_chat(chat_id, user_filter, search, request: gr.Request):
    auth.require_admin(request)
    if not chat_id:
        return (gr.update(), gr.update(), None, [], "Select a conversation to view it.", "⚠️ Select a chat first.",
                gr.update())
    db.delete_chat(chat_id)
    frame, chat_ids = chats_frame(user_filter, search)
    return frame, chat_ids, None, [], "Select a conversation to view it.", "✅ Chat deleted.", stats_html()


def delete_all_user_chats(user_filter, confirmed, search, request: gr.Request):
    auth.require_admin(request)
    if not user_filter:
        return gr.update(), gr.update(), "⚠️ Choose a specific user in the filter first.", gr.update(), gr.update()
    if not confirmed:
        return gr.update(), gr.update(), "⚠️ Tick the confirmation box first.", gr.update(), gr.update()
    removed = db.delete_user_chats(user_filter)
    frame, chat_ids = chats_frame(user_filter, search)
    return frame, chat_ids, f"✅ Deleted {removed} chat(s).", False, stats_html()


def refresh_moderation(user_filter, request: gr.Request):
    auth.require_admin(request)
    return moderation_frame(user_filter), stats_html()


def clear_moderation(confirmed, request: gr.Request):
    auth.require_admin(request)
    if not confirmed:
        return gr.update(), "⚠️ Tick the confirmation box first.", gr.update(), gr.update()
    removed = db.clear_moderation()
    return moderation_frame(0), f"✅ Cleared {removed} entr{'y' if removed == 1 else 'ies'}.", False, stats_html()

# =========================================
# Layout
# =========================================

with gr.Blocks(title="CodeGen Admin", css=CSS, theme=THEME, analytics_enabled=False) as admin_app:
    header_html = gr.HTML()
    stats = gr.HTML()

    with gr.Tabs(elem_id="admin-tabs"):

        with gr.Tab("Users"):
            users_table = gr.Dataframe(headers=USER_COLUMNS, interactive=False, wrap=True, elem_id="users-table")
            with gr.Row(equal_height=False):
                with gr.Column(elem_classes="cg-card"):
                    gr.Markdown("### Create user")
                    new_username = gr.Textbox(label="Username", placeholder="3-32 letters, digits, . _ -")
                    new_password = gr.Textbox(label="Password", type="password", placeholder="At least 8 characters")
                    new_role = gr.Radio(["user", "admin"], value="user", label="Role")
                    create_btn = gr.Button("Create user", variant="primary")

                with gr.Column(elem_classes="cg-card"):
                    gr.Markdown("### Edit user")
                    edit_user = gr.Dropdown(label="User", choices=[], value=None)
                    edit_role = gr.Radio(["user", "admin"], label="Role")
                    edit_active = gr.Checkbox(label="Account active")
                    edit_password = gr.Textbox(label="Reset password (optional)", type="password")
                    save_btn = gr.Button("Save changes", variant="primary")
                    with gr.Accordion("Delete user", open=False):
                        confirm_user_delete = gr.Checkbox(label="I understand this deletes the user and all of their chats")
                        delete_user_btn = gr.Button("Delete user", variant="stop")
            users_status = gr.Markdown()

        with gr.Tab("Chat history"):
            with gr.Row(equal_height=True):
                chat_user_filter = gr.Dropdown(label="User", choices=[(ALL_USERS, 0)], value=0, scale=1)
                chat_search = gr.Textbox(label="Search titles and messages", scale=2)
                search_btn = gr.Button("Search", scale=0, min_width=110)
            chats_status = gr.Markdown()
            chats_table = gr.Dataframe(headers=CHAT_COLUMNS, interactive=False, wrap=True, elem_id="chats-table")
            chat_ids_state = gr.State([])
            selected_chat = gr.State(None)
            with gr.Row(equal_height=False):
                transcript = gr.Chatbot(label="Conversation", height=520, latex_delimiters=[], show_copy_button=True,
                                        bubble_full_width=False, scale=3)
                with gr.Column(scale=1, elem_classes="cg-card"):
                    chat_meta = gr.Markdown("Select a conversation to view it.")
                    delete_chat_btn = gr.Button("Delete this chat", variant="stop")
                    with gr.Accordion("Delete all chats of the filtered user", open=False):
                        confirm_bulk_delete = gr.Checkbox(label="I understand this cannot be undone")
                        bulk_delete_btn = gr.Button("Delete all their chats", variant="stop")

        with gr.Tab("Moderation log"):
            with gr.Row(equal_height=True):
                mod_user_filter = gr.Dropdown(label="User", choices=[(ALL_USERS, 0)], value=0, scale=1)
                mod_refresh_btn = gr.Button("Refresh", scale=0, min_width=110)
            moderation_table = gr.Dataframe(headers=MODERATION_COLUMNS, interactive=False, wrap=True, elem_id="moderation-table")
            with gr.Accordion("Clear log", open=False):
                confirm_clear = gr.Checkbox(label="I understand this deletes every log entry")
                clear_btn = gr.Button("Clear moderation log", variant="stop")
            mod_status = gr.Markdown()

    # ---------------------
    # Events
    # ---------------------
    user_outputs = [users_table, edit_user, chat_user_filter, mod_user_filter, stats]

    admin_app.load(on_load, outputs=[header_html, *user_outputs, chats_table, chat_ids_state, moderation_table])

    create_btn.click(create_user, [new_username, new_password, new_role],
                     [users_status, new_username, new_password, *user_outputs])
    edit_user.input(select_user, [edit_user], [edit_role, edit_active, edit_password])
    save_btn.click(save_user, [edit_user, edit_role, edit_active, edit_password],
                   [users_status, edit_password, *user_outputs])
    delete_user_btn.click(delete_user, [edit_user, confirm_user_delete],
                          [users_status, confirm_user_delete, *user_outputs])

    chat_view_outputs = [chats_table, chat_ids_state, selected_chat, transcript, chat_meta, chats_status]
    for trigger in (search_btn.click, chat_search.submit, chat_user_filter.input):
        trigger(search_chats, [chat_user_filter, chat_search], chat_view_outputs)
    chats_table.select(view_chat, [chat_ids_state], [selected_chat, transcript, chat_meta])
    delete_chat_btn.click(delete_selected_chat, [selected_chat, chat_user_filter, chat_search],
                          chat_view_outputs + [stats])
    bulk_delete_btn.click(delete_all_user_chats, [chat_user_filter, confirm_bulk_delete, chat_search],
                          [chats_table, chat_ids_state, chats_status, confirm_bulk_delete, stats])

    for trigger in (mod_refresh_btn.click, mod_user_filter.input):
        trigger(refresh_moderation, [mod_user_filter], [moderation_table, stats])
    clear_btn.click(clear_moderation, [confirm_clear], [moderation_table, mod_status, confirm_clear, stats])
