#!/usr/bin/env python3
"""Open a local UI for testing source-document preview links from Postgres.

The UI samples one source document per retrieval source, chooses a random chunk
from that source's chunk table, and builds a reference link that loads
pre-generated preview bytes from ``public.aegis_source_documents``.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlparse

import psycopg2
from psycopg2 import sql

from retrieval_source_config import PROJECT_ROOT, SOURCES, RetrievalSource
from sync_env import first, read_env


PUBLIC_SCHEMA = "public"
DOCUMENTS_TABLE = "aegis_source_documents"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
PDF_MIME_TYPE = "application/pdf"
HTML_MIME_PREFIX = "text/html"


class PreviewUnavailableError(RuntimeError):
    """Raised when a document has not been pre-rendered for browser preview."""


@dataclass(frozen=True)
class AppState:
    """Shared immutable server configuration."""

    db_config: dict[str, str]
    env_file: Path
    quiet: bool


@dataclass(frozen=True)
class Reference:
    """One sampled source-document reference."""

    source_key: str
    source_label: str
    data_table: str
    source_type: str
    file_id: str
    filename: str
    file_type: str
    mime_type: str
    file_size: int
    fiscal_year: str
    quarter: str
    bank: str
    chunk_id: str
    page_number: int | None
    chunk_name: str
    summary: str
    chunk_preview: str
    preview_metadata: dict[str, Any]


@dataclass(frozen=True)
class SourceStatus:
    """Rendered status for one source card."""

    source_key: str
    source_label: str
    reference: Reference | None = None
    error: str | None = None


def main(argv: list[str] | None = None) -> int:
    """Run the local preview test server."""
    args = parse_args(argv)
    env_file = args.env_file.expanduser().resolve()
    values = read_env(env_file)
    state = AppState(
        db_config=database_config(values),
        env_file=env_file,
        quiet=args.quiet,
    )
    handler = make_handler(state)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"

    print(f"Serving source document preview test at {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping preview test server.")
    finally:
        server.server_close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="Root dotenv file with DB or POSTGRES settings.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Host interface for the local UI. Defaults to {DEFAULT_HOST}.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port for the local UI. Defaults to {DEFAULT_PORT}. Use 0 for any port.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the browser automatically.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-request access logs.",
    )
    return parser.parse_args(argv)


def database_config(values: Mapping[str, str]) -> dict[str, str]:
    """Return psycopg2 connection settings from the root dotenv values."""
    config = {
        "host": first(values, "DB_HOST", "POSTGRES_HOST", default="127.0.0.1"),
        "port": first(values, "DB_PORT", "POSTGRES_PORT", default="5432"),
        "dbname": first(values, "DB_NAME", "POSTGRES_DATABASE", default="postgres"),
        "user": first(values, "DB_USER", "POSTGRES_USER", default="postgres"),
        "password": first(values, "DB_PASSWORD", "POSTGRES_PASSWORD"),
    }
    return {key: value for key, value in config.items() if value}


def make_handler(state: AppState) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to the app state."""

    class PreviewHandler(BaseHTTPRequestHandler):
        server_version = "AegisDocumentPreviewTest/1.0"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"", "/"}:
                self.handle_index()
                return
            if parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            if parsed.path == "/healthz":
                self.send_text("ok\n", content_type="text/plain")
                return
            if parsed.path.startswith("/preview/"):
                self.handle_preview(parsed.path, parse_qs(parsed.query))
                return
            if parsed.path.startswith("/preview-document/"):
                self.handle_preview_document(parsed.path)
                return
            if parsed.path.startswith("/document/"):
                self.handle_document(parsed.path)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown route")

        def handle_index(self) -> None:
            statuses = load_source_statuses(state.db_config)
            self.send_html(render_index(statuses, state.env_file))

        def handle_preview(
            self,
            path: str,
            query: Mapping[str, list[str]],
        ) -> None:
            try:
                source, file_id = source_and_file_id_from_path(path, "preview")
                chunk_id = first_query_value(query, "chunk_id")
                reference = load_reference(state.db_config, source, file_id, chunk_id)
            except ValueError as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except LookupError as exc:
                self.send_error(HTTPStatus.NOT_FOUND, str(exc))
                return

            requested_page = parse_page_number(first_query_value(query, "page"))
            requested_sheet = first_query_value(query, "sheet")
            page_number = resolve_reference_page(
                reference,
                requested_page=requested_page,
                requested_sheet=requested_sheet,
            )
            self.send_html(
                render_preview(
                    reference,
                    page_number,
                    requested_page=requested_page,
                    requested_sheet=requested_sheet,
                )
            )

        def handle_preview_document(self, path: str) -> None:
            try:
                source, file_id = source_and_file_id_from_path(
                    path,
                    "preview-document",
                )
                payload = load_document_bytes(state.db_config, source, file_id)
                preview_payload = build_preview_payload(payload)
            except ValueError as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except LookupError as exc:
                self.send_error(HTTPStatus.NOT_FOUND, str(exc))
                return
            except PreviewUnavailableError as exc:
                self.send_error(HTTPStatus.CONFLICT, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return

            self.send_binary(
                content=preview_payload["content"],
                content_type=preview_payload["mime_type"],
                filename=preview_payload["filename"],
                disposition="inline",
            )

        def handle_document(self, path: str) -> None:
            try:
                source, file_id = source_and_file_id_from_path(path, "document")
                payload = load_document_bytes(state.db_config, source, file_id)
            except ValueError as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except LookupError as exc:
                self.send_error(HTTPStatus.NOT_FOUND, str(exc))
                return

            filename = safe_header_filename(payload["filename"])
            content = bytes(payload["original_bytes"])
            self.send_binary(
                content=content,
                content_type=payload["mime_type"],
                filename=filename,
                disposition="attachment",
            )

        def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_text(body, status=status, content_type="text/html; charset=utf-8")

        def send_text(
            self,
            body: str,
            *,
            status: HTTPStatus = HTTPStatus.OK,
            content_type: str,
        ) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_binary(
            self,
            *,
            content: bytes,
            content_type: str,
            filename: str,
            disposition: str,
        ) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header(
                "Content-Disposition",
                f'{disposition}; filename="{filename}"',
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: Any) -> None:
            if not state.quiet:
                super().log_message(format, *args)

    return PreviewHandler


def load_source_statuses(db_config: Mapping[str, str]) -> list[SourceStatus]:
    """Load one random document/chunk reference for each configured source."""
    statuses: list[SourceStatus] = []
    with psycopg2.connect(
        **db_config,
        application_name="aegis-source-document-preview-test",
    ) as conn:
        conn.autocommit = True
        for source in SOURCES.values():
            try:
                reference = load_random_reference(conn, source)
                if reference is None:
                    statuses.append(
                        SourceStatus(
                            source_key=source.key,
                            source_label=source.label,
                            error="No source document with chunk rows was found.",
                        )
                    )
                else:
                    statuses.append(
                        SourceStatus(
                            source_key=source.key,
                            source_label=source.label,
                            reference=reference,
                        )
                    )
            except Exception as exc:
                conn.rollback()
                statuses.append(
                    SourceStatus(
                        source_key=source.key,
                        source_label=source.label,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
    return statuses


def load_random_reference(
    conn: Any,
    source: RetrievalSource,
) -> Reference | None:
    """Pick a random source document, then a random chunk within that document."""
    query = sql.SQL(
        """
        WITH random_doc AS (
            SELECT
                d.source_type,
                d.file_id,
                d.filename,
                d.file_type,
                d.mime_type,
                d.file_size,
                d.fiscal_year,
                d.quarter,
                d.bank,
                d.preview_metadata
            FROM {documents} AS d
            WHERE EXISTS (
                SELECT 1
                FROM {data_table} AS c
                WHERE c.source_type = d.source_type
                  AND c.file_id = d.file_id
            )
            ORDER BY random()
            LIMIT 1
        )
        SELECT
            d.source_type,
            d.file_id,
            d.filename,
            d.file_type,
            d.mime_type,
            d.file_size,
            d.fiscal_year,
            d.quarter,
            d.bank,
            c.chunk_id,
            c.page_number,
            c.name,
            c.summary,
            left(coalesce(c.chunk_content, ''), 1200) AS chunk_preview,
            d.preview_metadata
        FROM random_doc AS d
        JOIN {data_table} AS c
          ON c.source_type = d.source_type
         AND c.file_id = d.file_id
        ORDER BY random()
        LIMIT 1
        """
    ).format(
        documents=table_ref(DOCUMENTS_TABLE),
        data_table=table_ref(source.data_table),
    )
    with conn.cursor() as cur:
        cur.execute(query)
        row = cur.fetchone()
    return reference_from_row(row, source) if row else None


def load_reference(
    db_config: Mapping[str, str],
    source: RetrievalSource,
    file_id: str,
    chunk_id: str,
) -> Reference:
    """Load a specific file/chunk reference for the preview frame."""
    query = sql.SQL(
        """
        SELECT
            d.source_type,
            d.file_id,
            d.filename,
            d.file_type,
            d.mime_type,
            d.file_size,
            d.fiscal_year,
            d.quarter,
            d.bank,
            c.chunk_id,
            c.page_number,
            c.name,
            c.summary,
            left(coalesce(c.chunk_content, ''), 1200) AS chunk_preview,
            d.preview_metadata
        FROM {documents} AS d
        JOIN {data_table} AS c
          ON c.source_type = d.source_type
         AND c.file_id = d.file_id
        WHERE d.file_id = %s
        ORDER BY CASE WHEN c.chunk_id = %s THEN 0 ELSE 1 END, random()
        LIMIT 1
        """
    ).format(
        documents=table_ref(DOCUMENTS_TABLE),
        data_table=table_ref(source.data_table),
    )
    with psycopg2.connect(
        **db_config,
        application_name="aegis-source-document-preview-test",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (file_id, chunk_id))
            row = cur.fetchone()
    if row is None:
        raise LookupError(f"No document/chunk found for {source.key}/{file_id}")
    return reference_from_row(row, source)


def load_document_bytes(
    db_config: Mapping[str, str],
    source: RetrievalSource,
    file_id: str,
) -> dict[str, Any]:
    """Load original bytes for one document scoped through a source table."""
    query = sql.SQL(
        """
        SELECT
            d.filename,
            d.mime_type,
            d.file_type,
            d.preview_mime_type,
            d.preview_bytes,
            d.original_bytes,
            d.preview_metadata,
            d.preview_error
        FROM {documents} AS d
        WHERE d.file_id = %s
          AND EXISTS (
              SELECT 1
              FROM {data_table} AS c
              WHERE c.source_type = d.source_type
                AND c.file_id = d.file_id
          )
        LIMIT 1
        """
    ).format(
        documents=table_ref(DOCUMENTS_TABLE),
        data_table=table_ref(source.data_table),
    )
    with psycopg2.connect(
        **db_config,
        application_name="aegis-source-document-preview-test",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (file_id,))
            row = cur.fetchone()
    if row is None:
        raise LookupError(f"No source document bytes found for {source.key}/{file_id}")
    return {
        "filename": str(row[0]),
        "mime_type": str(row[1]),
        "file_type": str(row[2]),
        "preview_mime_type": str(row[3]) if row[3] else "",
        "preview_bytes": row[4],
        "original_bytes": row[5],
        "preview_metadata": metadata_from_db(row[6]),
        "preview_error": str(row[7]) if row[7] else "",
    }


def build_preview_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return pre-generated preview bytes suitable for browser preview."""
    filename = str(payload["filename"])
    preview_mime_type = str(payload.get("preview_mime_type") or "")
    preview_bytes = payload.get("preview_bytes")
    preview_content = bytes(preview_bytes) if preview_bytes is not None else b""
    preview_error = str(payload.get("preview_error") or "")
    if preview_error:
        raise PreviewUnavailableError(
            "Preview generation previously failed for this document: "
            f"{preview_error}"
        )
    if not preview_mime_type or not preview_content:
        raise PreviewUnavailableError(
            "Preview bytes are missing. Run "
            "scripts/backfill_source_document_previews.py --all --apply --force."
        )
    normalized_mime = preview_mime_type.lower()
    if normalized_mime == PDF_MIME_TYPE:
        preview_filename = pdf_filename(filename)
    elif normalized_mime.startswith(HTML_MIME_PREFIX):
        preview_filename = html_filename(filename)
    else:
        raise PreviewUnavailableError(
            f"Unsupported preview MIME type: {preview_mime_type}"
        )
    return {
        "filename": preview_filename,
        "mime_type": preview_mime_type,
        "content": preview_content,
    }


def reference_from_row(row: tuple[Any, ...], source: RetrievalSource) -> Reference:
    """Convert a DB row into a UI reference."""
    return Reference(
        source_key=source.key,
        source_label=source.label,
        data_table=source.data_table,
        source_type=str(row[0]),
        file_id=str(row[1]),
        filename=str(row[2]),
        file_type=str(row[3]),
        mime_type=str(row[4]),
        file_size=int(row[5]),
        fiscal_year=str(row[6]),
        quarter=str(row[7]),
        bank=str(row[8]),
        chunk_id=str(row[9]),
        page_number=parse_page_number(row[10]),
        chunk_name=str(row[11] or ""),
        summary=str(row[12] or ""),
        chunk_preview=str(row[13] or ""),
        preview_metadata=metadata_from_db(row[14]),
    )


def render_index(statuses: list[SourceStatus], env_file: Path) -> str:
    """Render the main HTML UI."""
    cards = "\n".join(render_source_card(status) for status in statuses)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Aegis Source Document Preview Test</title>
  <style>{css()}</style>
</head>
<body>
  <header>
    <div>
      <h1>Aegis source document preview test</h1>
      <p>Random source documents and chunk-backed reference links from Postgres.</p>
    </div>
    <a class="button secondary" href="/">Refresh samples</a>
  </header>
  <main>
    <section class="sources" aria-label="Sampled source document references">
      <div class="meta">Using env file: <code>{escape(str(env_file))}</code></div>
      {cards}
    </section>
    <section class="preview-shell" aria-label="Preview window">
      <iframe
        name="preview-frame"
        title="Document preview"
        srcdoc="{escape(preview_placeholder(), quote=True)}"
      ></iframe>
    </section>
  </main>
</body>
</html>
"""


def render_source_card(status: SourceStatus) -> str:
    """Render one source card."""
    if status.reference is None:
        return f"""<article class="card error">
  <h2>{escape(status.source_label)}</h2>
  <p class="source-key">{escape(status.source_key)}</p>
  <p>{escape(status.error or "No sampled reference available.")}</p>
</article>"""

    ref = status.reference
    preview_url = reference_url(ref)
    direct_url = document_url(ref)
    locator_label, locator_value = reference_locator(ref)
    return f"""<article class="card">
  <div class="card-head">
    <div>
      <h2>{escape(ref.source_label)}</h2>
    <p class="source-key">{escape(ref.source_key)} &middot; {escape(ref.source_type)}</p>
    </div>
    <span>{escape(ref.file_type.upper())}</span>
  </div>
  <dl>
    <div><dt>Document</dt><dd>{escape(ref.filename)}</dd></div>
    <div>
      <dt>Bank / period</dt>
      <dd>{escape(ref.bank)} &middot; {escape(ref.fiscal_year)} {escape(ref.quarter)}</dd>
    </div>
    <div><dt>Chunk</dt><dd>{escape(ref.chunk_id)}</dd></div>
    <div><dt>{escape(locator_label)}</dt><dd>{escape(locator_value)}</dd></div>
    <div><dt>Bytes</dt><dd>{ref.file_size:,}</dd></div>
  </dl>
  <div class="actions">
    <a class="button" href="{escape(preview_url, quote=True)}" target="preview-frame">
      Open reference
    </a>
    <a class="button secondary" href="{escape(direct_url, quote=True)}" target="_blank">
      Download original
    </a>
  </div>
  <label>Reference link</label>
  <code class="link">{escape(preview_url)}</code>
  <p class="summary">{escape(ref.summary or ref.chunk_name or "No summary/name.")}</p>
  <pre>{escape(ref.chunk_preview)}</pre>
</article>"""


def render_preview(
    reference: Reference,
    page_number: int | None,
    requested_page: int | None,
    requested_sheet: str = "",
) -> str:
    """Render the clicked reference preview page."""
    doc_src = preview_document_url(reference, page_number=page_number)
    original_src = document_url(reference)
    page_text = str(page_number) if page_number is not None else "none"
    file_type = reference.file_type.lower()
    if file_type == "xlsx":
        locator_text = f"sheet {page_text}"
        if reference.chunk_name:
            locator_text += f" - {reference.chunk_name}"
    elif reference.file_type.lower() == "xml":
        locator_text = f"preview page {page_text}"
    else:
        locator_text = f"preview page {page_text}"
    preview_label = "Open HTML preview" if file_type == "xlsx" else "Open PDF preview"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(reference.filename)}</title>
  <style>{css()}</style>
</head>
<body class="preview-page">
  <div class="preview-toolbar">
    <div>
      <strong>{escape(reference.filename)}</strong>
      <span>{escape(reference.source_label)} &middot; {escape(locator_text)}</span>
    </div>
    <a class="button secondary" href="{escape(doc_src, quote=True)}" target="_blank">
      {escape(preview_label)}
    </a>
    <a class="button secondary" href="{escape(original_src, quote=True)}" target="_blank">
      Download original
    </a>
  </div>
  <iframe class="document-frame" src="{escape(doc_src, quote=True)}"></iframe>
</body>
</html>
"""


def preview_placeholder() -> str:
    """Return initial preview iframe content."""
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>
    body {
      align-items: center;
      color: #5f6b7a;
      display: flex;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      height: 100vh;
      justify-content: center;
      margin: 0;
    }
  </style>
</head>
<body>Select a reference to load source bytes from Postgres.</body>
</html>"""


def css() -> str:
    """Return compact CSS for the local diagnostic UI."""
    return """
:root {
  color-scheme: light;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
body {
  background: #f5f7f9;
  color: #17202c;
  margin: 0;
}
header {
  align-items: center;
  background: #ffffff;
  border-bottom: 1px solid #d9e0e7;
  display: flex;
  gap: 24px;
  justify-content: space-between;
  padding: 16px 20px;
}
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 0; }
p { margin: 0; }
header p, .meta, .source-key, dt, .preview-toolbar span {
  color: #5f6b7a;
  font-size: 12px;
}
main {
  display: grid;
  gap: 16px;
  grid-template-columns: minmax(380px, 520px) minmax(0, 1fr);
  height: calc(100vh - 73px);
  padding: 16px;
}
.sources {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  overflow: auto;
  padding-right: 4px;
}
.card {
  background: #ffffff;
  border: 1px solid #d9e0e7;
  border-radius: 8px;
  padding: 14px;
}
.card.error {
  border-color: #e3b0a8;
}
.card-head {
  align-items: start;
  display: flex;
  gap: 12px;
  justify-content: space-between;
  margin-bottom: 12px;
}
.card-head span {
  background: #edf6f2;
  border: 1px solid #c6ddd3;
  border-radius: 999px;
  color: #245c47;
  font-size: 11px;
  padding: 3px 8px;
}
dl {
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin: 0 0 12px;
}
dt { margin-bottom: 2px; }
dd {
  font-size: 13px;
  margin: 0;
  overflow-wrap: anywhere;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 10px 0;
}
.button {
  background: #214e72;
  border: 1px solid #214e72;
  border-radius: 6px;
  color: #ffffff;
  display: inline-flex;
  font-size: 13px;
  font-weight: 600;
  line-height: 1;
  padding: 9px 11px;
  text-decoration: none;
}
.button.secondary {
  background: #ffffff;
  color: #214e72;
}
label {
  color: #5f6b7a;
  display: block;
  font-size: 12px;
  margin: 10px 0 4px;
}
code.link, .meta code {
  background: #f0f3f6;
  border: 1px solid #d9e0e7;
  border-radius: 6px;
  display: block;
  font-size: 12px;
  overflow-wrap: anywhere;
  padding: 7px;
}
.summary {
  color: #334155;
  font-size: 13px;
  margin-top: 10px;
}
pre {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  color: #334155;
  font-size: 12px;
  margin: 10px 0 0;
  max-height: 130px;
  overflow: auto;
  padding: 8px;
  white-space: pre-wrap;
}
.preview-shell {
  background: #ffffff;
  border: 1px solid #d9e0e7;
  border-radius: 8px;
  min-width: 0;
  overflow: hidden;
}
.preview-shell iframe, .document-frame {
  border: 0;
  height: 100%;
  width: 100%;
}
.preview-page {
  background: #ffffff;
  height: 100vh;
  overflow: hidden;
}
.preview-toolbar {
  align-items: center;
  border-bottom: 1px solid #d9e0e7;
  display: flex;
  gap: 12px;
  height: 54px;
  justify-content: space-between;
  padding: 10px 12px;
}
.preview-toolbar div {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.document-frame {
  height: calc(100vh - 54px);
}
@media (max-width: 980px) {
  main {
    grid-template-columns: 1fr;
    height: auto;
  }
  .preview-shell {
    height: 70vh;
  }
}
"""


def source_and_file_id_from_path(
    path: str,
    route_name: str,
) -> tuple[RetrievalSource, str]:
    """Extract and validate source key plus file_id from a route path."""
    parts = path.strip("/").split("/", 2)
    if len(parts) != 3 or parts[0] != route_name:
        raise ValueError(f"Expected /{route_name}/<source>/<file_id>")
    source_key = unquote(parts[1])
    file_id = unquote(parts[2])
    if source_key not in SOURCES:
        raise ValueError(f"Unknown source: {source_key}")
    if not file_id:
        raise ValueError("file_id is required")
    return SOURCES[source_key], file_id


def reference_url(ref: Reference) -> str:
    """Return the UI preview URL for one reference."""
    url = (
        f"/preview/{quote(ref.source_key, safe='')}/{quote(ref.file_id, safe='')}"
        f"?chunk_id={quote(ref.chunk_id, safe='')}"
    )
    if ref.page_number is not None:
        url += f"&page={ref.page_number}"
    return url


def document_url(ref: Reference) -> str:
    """Return the byte-streaming URL for one source document."""
    return (
        f"/document/{quote(ref.source_key, safe='')}/{quote(ref.file_id, safe='')}"
        f"?chunk_id={quote(ref.chunk_id, safe='')}"
    )


def preview_document_url(ref: Reference, page_number: int | None = None) -> str:
    """Return the browser-preview URL for one source document."""
    url = (
        f"/preview-document/{quote(ref.source_key, safe='')}/"
        f"{quote(ref.file_id, safe='')}"
        f"?chunk_id={quote(ref.chunk_id, safe='')}"
    )
    resolved_page = page_number if page_number is not None else ref.page_number
    if resolved_page is not None:
        if ref.file_type.lower() == "xlsx":
            url += f"#sheet-{resolved_page}"
        else:
            url += f"#page={resolved_page}"
    return url


def resolve_reference_page(
    ref: Reference,
    *,
    requested_page: int | None,
    requested_sheet: str,
) -> int | None:
    """Resolve the preview page/sheet target for page- or sheet-based references."""
    file_type = ref.file_type.lower()
    sheet_name = requested_sheet or (
        ref.chunk_name if file_type == "xlsx" else ""
    )
    if sheet_name:
        sheet_page = preview_page_for_sheet(ref.preview_metadata, sheet_name)
        if sheet_page is not None:
            return sheet_page
    return requested_page or ref.page_number


def preview_page_for_sheet(
    preview_metadata: Mapping[str, Any],
    sheet_name: str,
) -> int | None:
    """Return the generated visible-sheet page number for an XLSX sheet name."""
    requested = sheet_name.casefold()
    for sheet in preview_metadata.get("sheets", []):
        if not isinstance(sheet, Mapping):
            continue
        if str(sheet.get("name", "")).casefold() != requested:
            continue
        return parse_page_number(
            sheet.get("page_number")
            or sheet.get("visible_sheet_number")
            or sheet.get("preview_page")
            or sheet.get("preview_start_page")
        )
    return None


def reference_locator(ref: Reference) -> tuple[str, str]:
    """Return a compact label/value for the sampled reference target."""
    if ref.file_type.lower() == "xlsx":
        page_label = str(ref.page_number) if ref.page_number is not None else "none"
        if ref.chunk_name:
            page_label += f" - {ref.chunk_name}"
        return "Sheet", page_label
    if ref.file_type.lower() == "xml":
        page_label = str(ref.page_number) if ref.page_number is not None else "none"
        return "Page", page_label
    page_label = str(ref.page_number) if ref.page_number is not None else "none"
    return "Page", page_label


def metadata_from_db(value: Any) -> dict[str, Any]:
    """Return JSONB metadata as a dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def first_query_value(query: Mapping[str, list[str]], key: str) -> str:
    """Return the first query-string value for a key."""
    values = query.get(key) or []
    return values[0] if values else ""


def parse_page_number(value: Any) -> int | None:
    """Return a positive page number, or None when missing/unusable."""
    if value in {None, ""}:
        return None
    try:
        page_number = int(value)
    except (TypeError, ValueError):
        return None
    return page_number if page_number > 0 else None


def safe_header_filename(filename: str) -> str:
    """Return a conservative filename for Content-Disposition."""
    return filename.replace("\\", "_").replace("/", "_").replace('"', "_")


def pdf_filename(filename: str) -> str:
    """Return a safe PDF preview filename."""
    return safe_header_filename(str(Path(filename).with_suffix(".pdf")))


def html_filename(filename: str) -> str:
    """Return a safe HTML preview filename."""
    return safe_header_filename(str(Path(filename).with_suffix(".html")))


def escape(value: Any, quote: bool = True) -> str:
    """HTML-escape a value."""
    return html.escape(str(value), quote=quote)


def table_ref(table_name: str) -> sql.Identifier:
    """Return a public-schema table reference."""
    return sql.Identifier(PUBLIC_SCHEMA, table_name)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
