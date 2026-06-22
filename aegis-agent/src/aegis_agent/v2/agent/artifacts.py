"""HTML artifact builders for V2 research output."""

from __future__ import annotations

from collections import defaultdict
from html import escape
from typing import Any
from urllib.parse import quote

from .models import EvidenceChunk, NormalizedTurn


def _scope_label(turn: NormalizedTurn) -> str:
    """Return a compact label for selected optional context."""
    parts: list[str] = []
    if turn.bank_symbols:
        parts.append(", ".join(turn.bank_symbols))
    if turn.fiscal_years or turn.quarters:
        periods = " ".join(
            [
                ", ".join(turn.quarters),
                ", ".join(str(year) for year in turn.fiscal_years),
            ]
        ).strip()
        if periods:
            parts.append(periods)
    if turn.source_ids:
        parts.append(f"{len(turn.source_ids)} selected source(s)")
    return " | ".join(parts) or "Unscoped search"


def evidence_ids(chunks: list[EvidenceChunk]) -> list[str]:
    """Return stable evidence ids for artifact references."""
    return [f"{chunk.source_name}:{chunk.chunk_id}" for chunk in chunks]


def _chunk_location(chunk: EvidenceChunk) -> str:
    """Return a human-readable location label for a chunk."""
    parts = [
        chunk.bank_ticker,
        chunk.quarter,
        str(chunk.fiscal_year) if chunk.fiscal_year else None,
        f"p. {chunk.page_number}" if chunk.page_number else None,
        chunk.sheet_name,
        chunk.section_name,
    ]
    return " / ".join(part for part in parts if part)


def _safe_int(value: Any) -> int | None:
    """Return an integer value when one is present."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _source_preview_href(
    source_name: str,
    file_id: str | None,
    page_number: int | None = None,
    sheet_name: str | None = None,
) -> str | None:
    """Return a preview URL for source-document links."""
    if not file_id:
        return None
    href = f"/source-documents/{quote(source_name, safe='')}/{quote(str(file_id), safe='')}/preview"
    if page_number:
        return f"{href}#page={page_number}"
    if sheet_name:
        return f"{href}#sheet-{quote(str(sheet_name), safe='')}"
    return href


def _source_download_href(source_name: str, file_id: str | None) -> str | None:
    """Return an original-byte download URL for source documents."""
    if not file_id:
        return None
    return f"/source-documents/{quote(source_name, safe='')}/{quote(str(file_id), safe='')}/download"


def _item_source_and_file(item: dict[str, Any]) -> tuple[str, str | None]:
    """Return source/file identifiers from a V1/V2 reference payload."""
    source_name = str(item.get("source_name") or item.get("source_id") or "").strip()
    file_id = item.get("file_id") or item.get("document_id")
    return source_name, str(file_id) if file_id else None


def _evidence_label(index: int) -> str:
    """Return the artifact-local evidence label."""
    return f"E{index}"


def _file_group_key(chunk: EvidenceChunk) -> tuple[str, str, str]:
    """Group chunks by file and coarse location."""
    return (
        chunk.file_id or chunk.file_name or "unknown-file",
        chunk.file_name or "Untitled source",
        chunk.sheet_name or "",
    )


def _chunk_actions(chunk: EvidenceChunk) -> str:
    """Render preview/download links for a chunk when metadata exists."""
    preview_href = _source_preview_href(
        chunk.source_name, chunk.file_id, chunk.page_number, chunk.sheet_name
    )
    download_href = _source_download_href(chunk.source_name, chunk.file_id)
    links = []
    if preview_href:
        links.append(f"<a href='{escape(preview_href)}'>Preview source</a>")
    if download_href:
        links.append(f"<a href='{escape(download_href)}'>Download original</a>")
    return f"<div class='actions'>{''.join(links)}</div>" if links else ""


def _citation_href(item: dict[str, Any]) -> str | None:
    """Return the best citation preview href."""
    href = str(item.get("href") or "").strip()
    if href:
        return href
    source_name, file_id = _item_source_and_file(item)
    if not source_name or not file_id:
        return None
    return _source_preview_href(
        source_name,
        file_id,
        _safe_int(item.get("page_number")),
        str(item.get("sheet_name") or "").strip() or None,
    )


def _citation_links(item: dict[str, Any]) -> str:
    """Render citation links when present."""
    href = _citation_href(item)
    source_name, file_id = _item_source_and_file(item)
    download_href = str(item.get("download_href") or "").strip()
    if not download_href and source_name and file_id:
        download_href = _source_download_href(source_name, file_id) or ""
    if not href and not download_href:
        return ""
    links = []
    if href:
        links.append(f"<a href='{escape(href)}'>Preview source</a>")
    if download_href:
        links.append(f"<a href='{escape(download_href)}'>Download original</a>")
    return f"<div class='actions'>{''.join(links)}</div>"


def _metric_line(metric: Any) -> str:
    """Render a metric payload for a finding."""
    if not isinstance(metric, dict):
        return ""
    parts = []
    metric_name = str(metric.get("metric_name") or "").strip()
    metric_value = str(metric.get("metric_value") or "").strip()
    unit = str(metric.get("unit") or "").strip()
    period = str(metric.get("period") or "").strip()
    segment = str(metric.get("segment") or "").strip()
    value = (
        f"{metric_value}{unit}"
        if unit in {"%", "x"}
        else " ".join(part for part in [metric_value, unit] if part)
    )
    if metric_name:
        parts.append(metric_name)
    if value:
        parts.append(value)
    if period:
        parts.append(period)
    if segment:
        parts.append(segment)
    return f"<p class='metric'>{escape(' | '.join(parts))}</p>" if parts else ""


def _evidence_ref_labels(finding: dict[str, Any]) -> str:
    """Render assigned evidence ids for a finding."""
    refs = finding.get("evidence_refs")
    if not isinstance(refs, list):
        return ""
    labels = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        evidence_id = str(ref.get("evidence_id") or "").strip()
        display = str(ref.get("display_label") or ref.get("filename") or "").strip()
        if evidence_id:
            labels.append(f"{evidence_id}{' - ' + display if display else ''}")
    if not labels:
        return ""
    return "<p class='evidence'>" + escape(" | ".join(labels)) + "</p>"


def _finding_table(finding: dict[str, Any]) -> str:
    """Render a compact finding table payload."""
    table = finding.get("table")
    if not isinstance(table, dict):
        return ""
    columns = [str(column) for column in table.get("columns") or []]
    rows = table.get("rows") if isinstance(table.get("rows"), list) else []
    if not columns and rows and isinstance(rows[0], dict):
        columns = [str(column) for column in rows[0].keys()]
    if not columns:
        return ""
    header = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body_rows = []
    for row in rows[:20]:
        if isinstance(row, dict):
            cells = [row.get(column, "") for column in columns]
        elif isinstance(row, list):
            cells = row[: len(columns)]
        else:
            continue
        body_rows.append(
            "<tr>"
            + "".join(f"<td>{escape(str(cell))}</td>" for cell in cells)
            + "</tr>"
        )
    title = str(table.get("title") or "").strip()
    notes = str(table.get("notes") or "").strip()
    return (
        f"{f'<h4>{escape(title)}</h4>' if title else ''}"
        f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
        f"{f'<p class=\"meta\">{escape(notes)}</p>' if notes else ''}"
    )


def _finding_article(item: dict[str, Any]) -> str:
    """Render one deep finding."""
    title = str(item.get("combo_label") or "Finding")
    summary = str(item.get("summary") or item.get("details") or "")
    details = str(item.get("details") or "").strip()
    return (
        "<article class='finding'>"
        f"<h3>{escape(title)}</h3>"
        f"{_evidence_ref_labels(item)}"
        f"{_metric_line(item.get('metric'))}"
        f"<p>{escape(summary)}</p>"
        f"{f'<p>{escape(details)}</p>' if details and details != summary else ''}"
        f"{_finding_table(item)}"
        "</article>"
    )


def _source_nav_label(source: str, chunks: list[EvidenceChunk]) -> str:
    """Return a source nav label with retained chunk count."""
    label = (
        chunks[0].source_display_name if chunks else source.replace("_", " ").title()
    )
    return f"{label} ({len(chunks)})"


def _artifact_styles() -> str:
    """Return shared artifact CSS."""
    return (
        "body{font-family:Inter,Arial,sans-serif;margin:0;color:#182230;background:#f7f9fc;line-height:1.42}"
        "header{background:#fff;border-bottom:1px solid #d8e1eb;padding:20px 24px 14px;position:sticky;top:0;z-index:2}"
        "main{padding:18px 24px 28px}h1{font-size:21px;margin:0 0 6px;line-height:1.22}"
        "h2{font-size:16px;margin:22px 0 8px}h3{font-size:13px;margin:0 0 4px}"
        "h4{font-size:12px;margin:14px 0 6px;color:#32445a}.scope,.meta{color:#617085;font-size:12px}"
        ".kicker{color:#0b6b8f;font-size:10px;font-weight:850;text-transform:uppercase;letter-spacing:.04em}"
        "nav{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}nav a,.actions a{border:1px solid #c9d6e3;"
        "background:#f9fbfd;padding:4px 7px;color:#084f6b;text-decoration:none;font-size:11px;font-weight:800;margin-right:6px}"
        ".source,.finding,.chunk,.gap{border:1px solid #d8e1eb;background:#fff;padding:11px;margin:9px 0}"
        ".file-group{border-left:3px solid #9db3c8;padding-left:10px;margin:10px 0 14px}"
        ".chunk p,.finding p,li{font-size:12px;white-space:pre-wrap}.actions{margin-top:8px}"
        ".evidence{color:#0b6b8f;font-size:10px;font-weight:850;text-transform:uppercase}.metric{font-size:12px;color:#344761}"
        "table{border-collapse:collapse;width:100%;margin:8px 0}th,td{border:1px solid #d8e1eb;padding:6px;font-size:12px;text-align:left}"
        "th{background:#edf3f8}.empty{background:#fff;border:1px dashed #c9d6e3;padding:14px;color:#617085}"
    )


def quick_research_html(
    turn: NormalizedTurn, chunks: list[EvidenceChunk], gaps: list[str]
) -> str:
    """Build a self-contained quick-search artifact."""
    grouped: dict[str, list[EvidenceChunk]] = defaultdict(list)
    for chunk in chunks:
        grouped[chunk.source_name].append(chunk)

    nav = "".join(
        f"<a href='#{escape(source)}'>{escape(_source_nav_label(source, source_chunks))}</a>"
        for source, source_chunks in grouped.items()
    )
    source_sections: list[str] = []
    for source, source_chunks in grouped.items():
        file_groups: dict[tuple[str, str, str], list[EvidenceChunk]] = defaultdict(list)
        for chunk in source_chunks:
            file_groups[_file_group_key(chunk)].append(chunk)
        file_sections: list[str] = []
        source_index = 0
        for (_file_id, file_name, sheet_name), file_chunks in file_groups.items():
            cards = []
            for chunk in file_chunks:
                source_index += 1
                evidence_id = f"{chunk.source_name}:{chunk.chunk_id}"
                cards.append(
                    "<article class='chunk'>"
                    f"<div class='evidence'>{escape(_evidence_label(source_index))} | {escape(evidence_id)}</div>"
                    f"<p class='meta'>{escape(_chunk_location(chunk) or chunk.source_display_name)}</p>"
                    f"<p>{escape(chunk.chunk_content)}</p>"
                    f"{_chunk_actions(chunk)}"
                    "</article>"
                )
            file_meta = (
                f"Sheet: {sheet_name}"
                if sheet_name
                else f"{len(file_chunks)} retained chunk(s)"
            )
            file_sections.append(
                "<div class='file-group'>"
                f"<h3>{escape(file_name)}</h3>"
                f"<p class='meta'>{escape(file_meta)}</p>"
                f"{''.join(cards)}"
                "</div>"
            )
        source_sections.append(
            f"<section class='source' id='{escape(source)}'><h2>{escape(source_chunks[0].source_display_name)}</h2>"
            f"<p class='meta'>{len(source_chunks)} retained chunk(s) across {len(file_groups)} file/location group(s)</p>"
            f"{''.join(file_sections)}</section>"
        )

    gap_html = ""
    if gaps:
        gap_html = (
            "<section><h2>Source gaps</h2>"
            + "".join(f"<article class='gap'>{escape(gap)}</article>" for gap in gaps)
            + "</section>"
        )

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Quick research - {escape(turn.content[:80])}</title>"
        f"<style>{_artifact_styles()}</style></head><body>"
        "<header>"
        "<div class='kicker'>Quick search artifact</div>"
        f"<h1>{escape(turn.content)}</h1>"
        f"<p class='scope'>{escape(_scope_label(turn))} | {len(chunks)} retained chunk(s)</p>"
        f"<nav>{nav}</nav>"
        "</header>"
        "<main>"
        f"{''.join(source_sections) if source_sections else '<section class=\"empty\"><h2>No evidence found</h2><p>No chunks matched the selected context.</p></section>'}"
        f"{gap_html}"
        "</main></body></html>"
    )


def deep_research_html(
    turn: NormalizedTurn,
    research_result: dict[str, Any],
    fallback_chunks: list[EvidenceChunk],
    gaps: list[str],
) -> str:
    """Build a self-contained deep-search artifact."""
    findings = (
        research_result.get("findings")
        if isinstance(research_result.get("findings"), list)
        else []
    )
    citations = (
        research_result.get("citations")
        if isinstance(research_result.get("citations"), list)
        else []
    )
    summary = str(
        research_result.get("quick_summary")
        or "Deep research completed with limited source output."
    )
    finding_html = "".join(
        _finding_article(item) for item in findings[:40] if isinstance(item, dict)
    )
    citation_html = "".join(
        "<li>"
        f"<strong>{escape(str(item.get('evidence_id') or item.get('display_label') or item.get('title') or item.get('filename') or 'Citation'))}</strong>"
        f" - {escape(str(item.get('text_excerpt') or '')[:500])}"
        f"{_citation_links(item)}"
        "</li>"
        for item in citations[:40]
        if isinstance(item, dict)
    )
    gap_html = "".join(f"<li>{escape(gap)}</li>" for gap in gaps)
    if not finding_html and fallback_chunks:
        finding_html = "".join(
            "<article class='finding'>"
            f"<h3>{escape(chunk.source_display_name)} - {escape(_chunk_location(chunk) or 'Evidence')}</h3>"
            f"<p>{escape(chunk.chunk_content[:1000])}</p>"
            f"{_chunk_actions(chunk)}"
            "</article>"
            for chunk in fallback_chunks[:24]
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Deep research - {escape(turn.content[:80])}</title>"
        f"<style>{_artifact_styles()}</style></head><body>"
        "<header>"
        "<div class='kicker'>Deep search artifact</div>"
        f"<h1>{escape(turn.content)}</h1>"
        f"<p class='scope'>{escape(_scope_label(turn))}</p>"
        f"<p>{escape(summary)}</p>"
        "</header>"
        "<main>"
        f"<section><h2>Findings</h2>{finding_html or '<p>No findings were returned.</p>'}</section>"
        f"<section><h2>Citations</h2><ul>{citation_html or '<li>No citations were returned.</li>'}</ul></section>"
        f"{'<section><h2>Source gaps</h2><ul>' + gap_html + '</ul></section>' if gap_html else ''}"
        "</main></body></html>"
    )
