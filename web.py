import asyncio
import threading
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import db
from config import settings

app = FastAPI(title="1812", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "bot": "1812"}


@app.get("/api/channels")
async def list_channels():
    channels = await db.get_all_channels()
    return {"channels": channels}


@app.get("/api/history/{channel_id}")
async def get_history(channel_id: int):
    history = await db.get_channel_history(channel_id)
    return {"channel_id": channel_id, "messages": history}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    channels = await db.get_all_channels()
    rows = "".join(
        f"<tr><td><a href='/api/history/{c['channel_id']}'>{c['channel_id']}</a></td></tr>"
        for c in channels
    )
    return f"""
    <html>
    <head><title>1812 Dashboard</title>
    <style>body{{font-family:monospace;background:#1a1a2e;color:#eee;padding:2rem}}
    table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #444;padding:.5rem}}
    a{{color:#7ec8e3}}</style></head>
    <body>
    <h1>1812 — Dashboard</h1>
    <h2>Active Channels</h2>
    <table><tr><th>Channel ID</th></tr>{rows}</table>
    <p><a href="/api/channels">JSON</a> · <a href="/health">Health</a></p>
    </body></html>
    """


def start_web():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())


def run_web_in_background():
    thread = threading.Thread(target=start_web, daemon=True)
    thread.start()
