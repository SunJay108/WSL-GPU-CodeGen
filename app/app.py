import os

os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

import logging

import gradio as gr
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

import auth
import db
from admin_ui import admin_app
from chat_ui import chat_app, warm_up

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAVICON = os.path.join(BASE_DIR, "static", "logo.png")

# =========================================
# Database & first admin
# =========================================

db.init_db()
auth.ensure_admin()

# =========================================
# Web app
# =========================================

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
auth.install_routes(app)
app.add_middleware(auth.PageGate)

chat_app.queue(default_concurrency_limit=8)
admin_app.queue(default_concurrency_limit=4)

# /admin must be mounted before / so it is matched first.
app = gr.mount_gradio_app(app, admin_app, path="/admin", auth_dependency=auth.admin_dependency)
app = gr.mount_gradio_app(app, chat_app, path="/",
                          auth_dependency=auth.chat_dependency, favicon_path=FAVICON)

# =========================================
# Launch
# =========================================

if __name__ == "__main__":
    warm_up()
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="warning")
