import os

os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Dict, List, Tuple


import requests
import gradio as gr
from config import SystemPrompt

from filter import validate_prompt

# =========================================
# llama.cpp Server Configuration
# =========================================

LLAMA_URL = os.getenv("LLAMA_URL", "http://llama-server:8080")
LLAMA_MODEL = os.getenv("LLAMA_MODEL", "qwen2.5-coder")

# =========================================
# Token Settings
# =========================================

TOTAL_CONTEXT = 4096
MAX_OUTPUT_TOKENS = 2048
SYSTEM_RESERVED = 200

# =========================================
# Database
# =========================================

DB_DIR = "/app/data"
os.makedirs(DB_DIR, exist_ok=True)
DB_FILE = os.path.join(DB_DIR, "chat_history.db")

conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT,
    role TEXT,
    content TEXT,
    created_at TEXT
)
""")

conn.commit()

# =========================================
# Types
# =========================================

History = List[Tuple[str, str]]
Messages = List[Dict[str, str]]

# =========================================
# Roles
# =========================================

class Role:
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"

# =========================================
# Helpers
# =========================================

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def safe_context_limit() -> int:
    return TOTAL_CONTEXT - MAX_OUTPUT_TOKENS - SYSTEM_RESERVED



def fix_markdown(text: str) -> str:
    """
    Prevent broken rendering caused by unfinished markdown code blocks.
    """
    if text.count("```") % 2 != 0:
        text += "\n```"

    return text

# =========================================
# Database Functions
# =========================================

def create_chat(first_message="New Chat"):
    chat_id = str(uuid.uuid4())
    title = first_message[:50].strip()

    if not title:
        title = f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    cursor.execute("""
        INSERT INTO chats (id, title, created_at)
        VALUES (?, ?, ?)
    """, (chat_id, title, datetime.now().isoformat()))

    conn.commit()
    return chat_id


def save_message(chat_id, role, content):
    cursor.execute("""
        INSERT INTO messages (chat_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
    """, (chat_id, role, content, datetime.now().isoformat()))

    conn.commit()


def load_chat(chat_id):
    cursor.execute("""
        SELECT role, content
        FROM messages
        WHERE chat_id=?
        ORDER BY id ASC
    """, (chat_id,))

    rows = cursor.fetchall()
    history = []
    user_msg = None

    for role, content in rows:
        if role == Role.USER:
            user_msg = content
        elif role == Role.ASSISTANT and user_msg:
            history.append((user_msg, content))
            user_msg = None

    return history


def list_chats():
    cursor.execute("""
        SELECT id, title
        FROM chats
        ORDER BY created_at DESC
    """)
    return cursor.fetchall()


def delete_chat(chat_id):
    cursor.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
    cursor.execute("DELETE FROM chats WHERE id=?", (chat_id,))
    conn.commit()

def confirm_delete(chat_id):
    if chat_id:
        delete_chat(chat_id)

    return (
        [], [], None,
        refresh_chat_list(),
        gr.update(visible=False),
        None
    )

def cancel_delete():
    return gr.update(visible=False), None


# =========================================
# Message Conversion
# =========================================

def history_to_messages(history, system_prompt):
    messages = [{"role": Role.SYSTEM, "content": system_prompt}]

    for q, a in history:
        messages.append({"role": Role.USER, "content": q})
        messages.append({"role": Role.ASSISTANT, "content": a})

    return messages


def trim_messages(messages):
    system_msg = messages[0]
    trimmed = [system_msg]
    total_tokens = estimate_tokens(system_msg["content"])

    for msg in reversed(messages[1:]):
        msg_tokens = estimate_tokens(msg["content"])

        if total_tokens + msg_tokens > safe_context_limit():
            break

        trimmed.insert(1, msg)
        total_tokens += msg_tokens

    return trimmed

# =========================================
# Stop Generation
# =========================================

stop_flag = {"stop": False}

def stop_generation():
    stop_flag["stop"] = True

# =========================================
# Chat List Refresh
# =========================================

def refresh_chat_list():
    chats = list_chats()
    return gr.update(choices=[(title, chat_id) for chat_id, title in chats])

# =========================================
# Generate
# =========================================

def generate(query, history, chat_id):
    system_prompt = SystemPrompt
    valid, error = validate_prompt(query)

    if not valid:
        yield (history + [(query, error)], history, chat_id, refresh_chat_list())
        return

    stop_flag["stop"] = False

    if not chat_id:
        chat_id = create_chat(query)

    save_message(chat_id, Role.USER, query)

    messages = history_to_messages(history, system_prompt)
    messages.append({"role": Role.USER, "content": query})
    messages = trim_messages(messages)

    payload = {
        "model": LLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": 0.2,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }

    full_text = ""

    try:
        with requests.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json=payload,
            stream=True,
            timeout=1200,
        ) as response:

            response.raise_for_status()

            # llama.cpp OpenAI-compatible streaming uses Server-Sent Events.
            for line in response.iter_lines(decode_unicode=True):
                if stop_flag["stop"]:
                    final = fix_markdown(full_text + "\n\n⛔ Generation stopped.")
                    yield (history + [(query, final)], history, chat_id, refresh_chat_list())
                    return

                if not line:
                    continue

                if not line.startswith("data:"):
                    continue

                event_data = line[5:].strip()

                if event_data == "[DONE]":
                    break

                data = json.loads(event_data)
                choices = data.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                chunk = delta.get("content") or ""

                if not chunk:
                    continue

                full_text += chunk

                yield (history + [(query, fix_markdown(full_text))], history, chat_id, refresh_chat_list())

    except Exception as e:
        yield (history + [(query, f"❌ Error: {str(e)}")], history, chat_id, refresh_chat_list())
        return

    full_text = fix_markdown(full_text)
    save_message(chat_id, Role.ASSISTANT, full_text)
    history.append((query, full_text))

    yield (history, history, chat_id, refresh_chat_list())

# =========================================
# Chat Controls
# =========================================

def load_selected_chat(chat_id):
    if not chat_id:
        return [], []
    history = load_chat(chat_id)
    return history, history


def new_chat():
    return [], [], None, refresh_chat_list(), ""


def remove_chat(chat_id):
    if chat_id:
        delete_chat(chat_id)
    return [], [], None, refresh_chat_list()
    

# =========================================
# UI
# =========================================

css = """
html, body {
    margin: 0;
    padding: 0;
}

.gradio-container {
    max-width: 1500px !important;
    margin: 0 auto !important;
    padding: 12px 18px 18px 18px !important;
}

/* -------------------------
   Header / logo
-------------------------- */

#logo-wrap {
    display: flex !important;
    justify-content: center !important;
    align-items: center !important;
    padding: 4px 0 12px 0 !important;
}

#logo img {
    width: clamp(140px, 16vw, 260px) !important;
    max-width: 260px !important;
    min-width: 120px !important;
    height: auto !important;
    object-fit: contain !important;
}

/* -------------------------
   Main layout
-------------------------- */

#main-layout {
    display: flex !important;
    flex-direction: row !important;
    flex-wrap: nowrap !important;

    width: 100% !important;
    gap: 16px !important;

    align-items: flex-start !important;
}

/* -------------------------
   Sidebar
-------------------------- */

#sidebar {
    min-width: 220px !important;
    max-width: 280px !important;
    flex: 0 0 250px !important;

    padding: 12px !important;
    border-radius: 12px !important;

    /* Keep sidebar fixed while chat content grows */
    position: sticky !important;
    top: 12px !important;
    align-self: flex-start !important;
    height: fit-content !important;

    z-index: 20 !important;
}

#sidebar-actions {
    display: flex !important;
    flex-direction: column !important;
    width: 100% !important;
    gap: 8px !important;
    margin-bottom: 12px !important;
}

#new_chat_btn,
#delete_chat_btn {
    display: block !important;
    width: 100% !important;
    min-width: 100% !important;
    max-width: 100% !important;
    flex: none !important;

    height: 44px !important;
    min-height: 44px !important;
    padding: 0 !important;

    background-size: 22px 22px !important;
    background-repeat: no-repeat !important;
    background-position: center !important;

    border-radius: 8px !important;
}

#new_chat_btn {
    background-image: url("/file=/app/app/new_chat.png") !important;
}

#delete_chat_btn {
    background-image: url("/file=/app/app/del_chat.png") !important;
}

#chat-selector {
    width: 100% !important;
}

/* -------------------------
   Main chat panel
-------------------------- */

#chat-panel {
    flex: 1 1 0 !important;

    width: 0 !important;
    min-width: 0 !important;
    max-width: 100% !important;

    overflow: hidden !important;
}

#chatbot {
    width: 100% !important;

    height: calc(100vh - 330px) !important;
    min-height: 420px !important;
    max-height: calc(100vh - 330px) !important;

    overflow-y: auto !important;
    overflow-x: hidden !important;
}

/* Prevent long saved-chat content from expanding the flex row */
#chatbot * {
    min-width: 0 !important;
}

#chatbot pre {
    max-width: 100% !important;
    overflow-x: auto !important;
    white-space: pre !important;
}

#chatbot code {
    max-width: 100% !important;
}

#chatbot .message {
    max-width: 100% !important;
    overflow-wrap: anywhere !important;
    word-break: break-word !important;
}

#prompt-box textarea {
    min-height: 90px !important;
    resize: vertical !important;
}

#action-row {
    gap: 8px !important;
}

/* -------------------------
   Tablet
-------------------------- */

@media (max-width: 900px) {
    .gradio-container {
        padding: 10px !important;
    }

    #main-layout {
        flex-direction: column !important;
        flex-wrap: nowrap !important;
    }

    #sidebar {
        width: 100% !important;
        min-width: 0 !important;
        max-width: none !important;
        flex: none !important;
    }

    #chat-panel {
        width: 100% !important;
        min-width: 0 !important;
        max-width: 100% !important;
        flex: none !important;
        overflow: hidden !important;
    }


    #sidebar-actions {
        flex-direction: column !important;
        width: 100% !important;
    }

    #new_chat_btn,
    #delete_chat_btn {
        width: 100% !important;
        min-width: 100% !important;
        max-width: 100% !important;
    }

    #chatbot {
        height: 55vh !important;
        min-height: 380px !important;
    }

    #logo img {
        width: clamp(120px, 26vw, 210px) !important;
    }
}

/* -------------------------
   Mobile
-------------------------- */

@media (max-width: 600px) {
    .gradio-container {
        padding: 6px !important;
    }

    #logo-wrap {
        padding-bottom: 6px !important;
    }

    #logo img {
        width: 135px !important;
    }

    #sidebar-actions {
        flex-direction: column !important;
        width: 100% !important;
    }

    #new_chat_btn,
    #delete_chat_btn {
        width: 100% !important;
        min-width: 100% !important;
        max-width: 100% !important;
        height: 40px !important;
        min-height: 40px !important;
    }

    #chatbot {
        height: 50vh !important;
        min-height: 320px !important;
    }

    #action-row {
        flex-direction: column !important;
    }

    #action-row button {
        width: 100% !important;
    }
}

/* -------------------------
   Delete confirmation overlay
-------------------------- */

#delete-confirm {
    position: fixed !important;

    left: 50% !important;
    top: 50% !important;
    transform: translate(-50%, -50%) !important;

    width: min(420px, 90vw) !important;
    max-width: 420px !important;

    padding: 20px !important;

    border: 1px solid var(--border-color-primary) !important;
    border-radius: 12px !important;

    background: var(--background-fill-primary) !important;

    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.45) !important;

    z-index: 9999 !important;
}

footer {
    display: none !important;
}
"""

with gr.Blocks(
    title="Offline CodeGen AI",
    css=css,
    analytics_enabled=False,
) as demo:

    # =========================
    # Header
    # =========================

    with gr.Row(elem_id="logo-wrap"):
        gr.Image(
            value="logo.png",
            show_label=False,
            interactive=False,
            container=False,
            show_download_button=False,
            elem_id="logo",
        )

    # =========================
    # Main layout
    # =========================

    with gr.Row(elem_id="main-layout"):

        # ---------------------
        # Sidebar
        # ---------------------

        with gr.Column(
            scale=1,
            elem_id="sidebar",
        ):

            with gr.Row(elem_id="sidebar-actions"):
                new_btn = gr.Button(
                    "",
                    elem_id="new_chat_btn",
                )

                delete_btn = gr.Button(
                    "",
                    elem_id="delete_chat_btn",
                )

            chat_selector = gr.Dropdown(
                label="Saved Chats",
                choices=[],
                interactive=True,
                elem_id="chat-selector",
            )

        # ---------------------
        # Chat area
        # ---------------------

        with gr.Column(
            scale=4,
            elem_id="chat-panel",
        ):

            chatbot = gr.Chatbot(
                show_label=False,
                container=True,
                elem_id="chatbot",
            )

            input_box = gr.Textbox(
                placeholder="Describe your software or code...",
                lines=3,
                show_label=False,
                elem_id="prompt-box",
            )

            with gr.Row(elem_id="action-row"):
                send_btn = gr.Button(
                    "Generate",
                    variant="primary",
                )
                stop_btn = gr.Button("Stop")
                clear_btn = gr.Button("Clear")

    # =========================
    # Delete confirmation overlay
    # =========================

    confirm_box = gr.Column(
        visible=False,
        elem_id="delete-confirm",
    )

    with confirm_box:
        gr.Markdown("### ⚠️ Confirm Delete?")
        gr.Markdown("This action cannot be undone.")

        with gr.Row():
            confirm_yes = gr.Button(
                "Yes, Delete",
                variant="stop",
            )
            confirm_no = gr.Button("Cancel")

    # =========================
    # States
    # =========================

    state = gr.State([])
    chat_id_state = gr.State(None)
    delete_pending_chat = gr.State(None)

    # =========================
    # Events
    # =========================

    demo.load(refresh_chat_list, outputs=chat_selector)

    send_btn.click(
        fn=generate,
        inputs=[input_box, state, chat_id_state],
        outputs=[chatbot, state, chat_id_state, chat_selector],
    )

    stop_btn.click(fn=stop_generation)

    clear_btn.click(
        fn=new_chat,
        outputs=[chatbot, state, chat_id_state, chat_selector, input_box],
    )

    new_btn.click(
        fn=new_chat,
        outputs=[chatbot, state, chat_id_state, chat_selector, input_box],
    )

    chat_selector.change(
        fn=load_selected_chat,
        inputs=chat_selector,
        outputs=[chatbot, state],
    ).then(
        lambda x: x,
        inputs=chat_selector,
        outputs=chat_id_state,
    )

    delete_btn.click(
        fn=lambda chat_id: (gr.update(visible=True), chat_id),
        inputs=chat_selector,
        outputs=[confirm_box, delete_pending_chat],
    )

    confirm_yes.click(
        fn=confirm_delete,
        inputs=delete_pending_chat,
        outputs=[
            chatbot,
            state,
            chat_id_state,
            chat_selector,
            confirm_box,
            delete_pending_chat,
        ],
    )

    confirm_no.click(
        fn=cancel_delete,
        outputs=[confirm_box, delete_pending_chat],
    )

# =========================================
# Launch
# =========================================

if __name__ == "__main__":
    demo.queue().launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        allowed_paths=["/app/app"]
    )