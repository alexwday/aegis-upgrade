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
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from aegis_agent.connections.llm_connector import close_all_clients  # noqa: E402
from aegis_agent.connections.postgres_connector import close_all_connections, fetch_one  # noqa: E402
from aegis_agent.model.main import model  # noqa: E402
from aegis_agent.utils.logging import get_logger, setup_logging  # noqa: E402


setup_logging()
logger = get_logger()

SOURCE_FILTER_IDS = {
    "transcripts",
    "event_transcripts",
    "investor_slides",
    "supplementary_financials",
    "rts",
    "pillar3",
}


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


def _safe_header_filename(filename: str) -> str:
    """Return a conservative filename for Content-Disposition."""
    return filename.replace("\\", "_").replace("/", "_").replace('"', "_")


def _bytes_from_db(value: Any) -> bytes:
    """Normalize bytea values returned by async drivers."""
    if value is None:
        return b""
    if isinstance(value, memoryview):
        return value.tobytes()
    return bytes(value)


def _preview_filename(filename: str, mime_type: str) -> str:
    """Return a browser-friendly filename for preview bytes."""
    if mime_type.lower() == "application/pdf":
        return _safe_header_filename(str(Path(filename).with_suffix(".pdf")))
    if mime_type.lower().startswith("text/html"):
        return _safe_header_filename(str(Path(filename).with_suffix(".html")))
    return _safe_header_filename(filename)


async def _load_source_document(source: str, file_id: str) -> Dict[str, Any]:
    """Load one source document byte row from Postgres."""
    if source not in SOURCE_FILTER_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}")
    row = await fetch_one(
        """
        SELECT
            filename,
            mime_type,
            preview_mime_type,
            preview_bytes,
            original_bytes,
            preview_error
        FROM public.aegis_source_documents
        WHERE source_type = :source
          AND file_id = :file_id
        LIMIT 1
        """,
        {"source": source, "file_id": file_id},
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Source document not found")
    return row


@app.get("/source-documents/{source}/{file_id}/preview")
async def source_document_preview(source: str, file_id: str) -> Response:
    """Stream pre-generated browser preview bytes for a source document."""
    row = await _load_source_document(source, file_id)
    preview_error = str(row.get("preview_error") or "")
    if preview_error:
        raise HTTPException(status_code=409, detail=preview_error)

    content = _bytes_from_db(row.get("preview_bytes"))
    mime_type = str(row.get("preview_mime_type") or "")
    if not content or not mime_type:
        raise HTTPException(status_code=409, detail="Preview bytes are missing")

    filename = _preview_filename(str(row.get("filename") or file_id), mime_type)
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'inline; filename="{quote(filename)}"',
        },
    )


@app.get("/source-documents/{source}/{file_id}/download")
async def source_document_download(source: str, file_id: str) -> Response:
    """Stream exact original source bytes as an attachment."""
    row = await _load_source_document(source, file_id)
    content = _bytes_from_db(row.get("original_bytes"))
    if not content:
        raise HTTPException(status_code=404, detail="Original bytes are missing")

    filename = _safe_header_filename(str(row.get("filename") or file_id))
    mime_type = str(row.get("mime_type") or "application/octet-stream")
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{quote(filename)}"',
        },
    )


def _user_message_from_payload(payload: Dict[str, Any]) -> Dict[str, str]:
    """Normalize websocket payloads into conversation messages."""
    content = str(payload.get("content") or "").strip()
    if not content and payload.get("ui_selection"):
        content = str(payload["ui_selection"].get("label") or "").strip()
    return {"role": "user", "content": content}


def _source_filter_from_payload(payload: Dict[str, Any]) -> List[str] | None:
    """Return a validated per-turn source filter from the websocket payload."""
    raw_sources = payload.get("source_filter")
    if not isinstance(raw_sources, list):
        return None

    selected: List[str] = []
    for source in raw_sources:
        normalized = str(source or "").strip()
        if normalized in SOURCE_FILTER_IDS and normalized not in selected:
            selected.append(normalized)

    return selected or None


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Handle one websocket chat session."""
    await websocket.accept()
    conversation_state: Dict[str, List[Dict[str, str]]] = {"messages": []}
    session_chart_artifacts: Dict[str, Dict[str, Any]] = {}
    session_evidence_registry: Dict[str, Dict[str, Any]] = {}
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

            source_filter = _source_filter_from_payload(payload)
            conversation_state["messages"].append(user_message)
            await websocket.send_json({"type": "status", "name": "system", "content": "Thinking"})

            assistant_parts: List[str] = []
            latest_card_question = ""

            async for event in model(
                conversation_state,
                source_filter=source_filter,
                prior_chart_artifacts=dict(session_chart_artifacts),
                prior_evidence_registry=dict(session_evidence_registry),
            ):
                await websocket.send_json(event)
                event_type = event.get("type")
                content = event.get("content")
                metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
                if event_type == "agent" and isinstance(content, str):
                    assistant_parts.append(content)
                elif event_type == "ui_card" and isinstance(content, dict):
                    latest_card_question = str(content.get("question") or "")
                elif event_type == "chart_artifact" and isinstance(content, dict):
                    chart_id = str(content.get("chart_id") or "").strip()
                    if chart_id:
                        session_chart_artifacts[chart_id] = content

                evidence_registry = metadata.get("evidence_registry")
                if isinstance(evidence_registry, dict) and metadata.get("research_result") is True:
                    session_chart_artifacts = {}
                    session_evidence_registry = evidence_registry

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
