"""Velocity Command Centre (§14): FastAPI + SSE on 127.0.0.1, event-bus and
ledger data ONLY — no computed promises, environments never merged."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

StatusProvider = Callable[[], dict[str, Any]]
RowsProvider = Callable[[str | None, int], list[dict[str, Any]]]

_PAGE = """<!doctype html>
<html><head><title>AEGIS VELOCITY — Command Centre</title>
<style>
 body{font-family:system-ui;margin:2rem;background:#0b0e14;color:#e6e6e6}
 h1{font-size:1.2rem} .badge{padding:2px 8px;border-radius:4px;background:#274}
 .badge.live{background:#a22} table{border-collapse:collapse;margin-top:1rem}
 td,th{border:1px solid #333;padding:4px 10px;font-size:0.85rem}
 #status{white-space:pre-wrap;font-family:monospace;font-size:0.8rem}
</style></head>
<body>
<h1>AEGIS VELOCITY <span id="mode" class="badge">…</span></h1>
<div id="status">connecting…</div>
<script>
 const es = new EventSource('/events');
 es.onmessage = (e) => {
   const s = JSON.parse(e.data);
   document.getElementById('mode').textContent = s.mode || '?';
   document.getElementById('mode').className = 'badge' +
     ((s.mode||'').startsWith('LIVE') ? ' live' : '');
   document.getElementById('status').textContent = JSON.stringify(s, null, 2);
 };
</script>
</body></html>"""


def create_app(status_provider: StatusProvider, rows_provider: RowsProvider) -> FastAPI:
    app = FastAPI(title="AEGIS VELOCITY", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _PAGE

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return status_provider()

    @app.get("/api/ledger")
    async def ledger_rows(kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return rows_provider(kind, min(limit, 1000))

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def stream() -> Any:
            while True:
                payload = json.dumps(status_provider(), default=str)
                yield f"data: {payload}\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
