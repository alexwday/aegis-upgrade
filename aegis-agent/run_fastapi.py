#!/usr/bin/env python
"""
FastAPI websocket app for the Aegis Agent demo.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from aegis_agent.connections.llm_connector import close_all_clients  # noqa: E402
from aegis_agent.connections.postgres_connector import close_all_connections  # noqa: E402
from aegis_agent.model.main import model  # noqa: E402
from aegis_agent.utils.logging import get_logger, setup_logging  # noqa: E402


setup_logging()
logger = get_logger()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Manage server startup and shutdown."""
    logger.info("fastapi.startup", message="Aegis Agent server starting")
    yield
    logger.info("fastapi.shutdown", message="Aegis Agent server stopping")
    await close_all_clients()
    await close_all_connections()


app = FastAPI(
    title="Aegis Agent",
    description="Four-source Aegis agent demo with websocket streaming",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates_dir = ROOT_DIR / "templates"
if templates_dir.exists():
    app.mount("/static", StaticFiles(directory=str(templates_dir)), name="static")


@app.get("/")
async def root() -> FileResponse:
    """Serve the static chat UI."""
    return FileResponse(templates_dir / "chat.html")


@app.get("/health")
async def health() -> JSONResponse:
    """Basic health endpoint."""
    return JSONResponse({"status": "ok", "service": "aegis-agent"})


def _user_message_from_payload(payload: Dict[str, Any]) -> Dict[str, str]:
    """Normalize websocket payloads into conversation messages."""
    content = str(payload.get("content") or "").strip()
    if not content and payload.get("ui_selection"):
        content = str(payload["ui_selection"].get("label") or "").strip()
    return {"role": "user", "content": content}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Handle one websocket chat session."""
    await websocket.accept()
    conversation_state: Dict[str, List[Dict[str, str]]] = {"messages": []}
    await websocket.send_json({"type": "status", "name": "system", "content": "Connected"})

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"type": "message", "content": raw}

            user_message = _user_message_from_payload(payload)
            if not user_message["content"]:
                await websocket.send_json(
                    {"type": "error", "name": "system", "content": "Empty message received."}
                )
                continue

            conversation_state["messages"].append(user_message)
            await websocket.send_json({"type": "status", "name": "system", "content": "Thinking"})

            assistant_parts: List[str] = []
            latest_card_question = ""

            async for event in model(conversation_state):
                await websocket.send_json(event)
                event_type = event.get("type")
                content = event.get("content")
                if event_type == "agent" and isinstance(content, str):
                    assistant_parts.append(content)
                elif event_type == "ui_card" and isinstance(content, dict):
                    latest_card_question = str(content.get("question") or "")

            if assistant_parts:
                conversation_state["messages"].append(
                    {"role": "assistant", "content": "".join(assistant_parts)}
                )
            elif latest_card_question:
                conversation_state["messages"].append(
                    {"role": "assistant", "content": latest_card_question}
                )

            await websocket.send_json({"type": "status", "name": "system", "content": "Ready"})
    except WebSocketDisconnect:
        logger.info("websocket.disconnected")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("websocket.failed", error=str(exc))
        await websocket.send_json({"type": "error", "name": "system", "content": str(exc)})


def main() -> None:
    """Run the development server."""
    parser = argparse.ArgumentParser(description="Run Aegis Agent FastAPI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        "run_fastapi:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
