#!/usr/bin/env python3
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "docs" / "architecture" / "runtime-schema"
OUTPUT_PATH = SOURCE_DIR / "runtime_schema.md"
SCHEMA_FILES = [
    ("Catalog", "catalog.csv"),
    ("Content", "content.csv"),
    ("Runtime", "runtime.csv"),
    ("UI Contract", "contract.csv"),
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def md_escape(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("|", "\\|")


def make_table(rows: list[dict[str, str]]) -> list[str]:
    lines = [
        "| Field | Type | Required | Key | Usage | Links | Status | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {field} | {type} | {required} | {key} | {usage} | {links} | {status} | {notes} |".format(
                field=md_escape(row.get("field")),
                type=md_escape(row.get("type")),
                required=md_escape(row.get("required")),
                key=md_escape(row.get("key_type")),
                usage=md_escape(row.get("ui_runtime_usage")),
                links=md_escape(row.get("linked_with")),
                status=md_escape(row.get("status")),
                notes=md_escape(row.get("notes")),
            )
        )
    return lines


def make_contract_table(rows: list[dict[str, str]]) -> list[str]:
    lines = [
        "| UI Event | Trigger | Method | Endpoint | Request Payload | Response Payload | Stream Events | Tables Read | Tables Written | UI Behavior | Developer Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {event} | {trigger} | {method} | {endpoint} | {request} | {response} | {stream} | {reads} | {writes} | {behavior} | {notes} |".format(
                event=md_escape(row.get("ui_event")),
                trigger=md_escape(row.get("ui_trigger")),
                method=md_escape(row.get("method")),
                endpoint=md_escape(row.get("api_endpoint")),
                request=md_escape(row.get("request_payload")),
                response=md_escape(row.get("response_payload")),
                stream=md_escape(row.get("stream_events")),
                reads=md_escape(row.get("tables_read")),
                writes=md_escape(row.get("tables_written")),
                behavior=md_escape(row.get("ui_behavior")),
                notes=md_escape(row.get("developer_notes")),
            )
        )
    return lines


def main() -> None:
    sections = read_csv(SOURCE_DIR / "sections.csv")
    section_order = {row["section"]: int(row["order"]) for row in sections}
    section_notes = {row["section"]: row["section_notes"] for row in sections}

    lines: list[str] = [
        "# Aegis Runtime Schema Field Map",
        "",
        "Generated from the CSV files in this folder. Edit the CSVs, then rerun `python3 scripts/render_runtime_schema_md.py` from the repo root.",
        "",
        "## Section Notes",
        "",
        "| Section | Notes |",
        "| --- | --- |",
    ]

    for row in sorted(sections, key=lambda item: int(item["order"])):
        lines.append(f"| {md_escape(row['section'])} | {md_escape(row['section_notes'])} |")

    for schema_title, filename in SCHEMA_FILES:
        rows = read_csv(SOURCE_DIR / filename)
        if rows and "ui_event" in rows[0]:
            lines.extend(["", f"## {schema_title}", ""])
            lines.extend(make_contract_table(rows))
            lines.append("")
            continue

        grouped: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
        for row in rows:
            grouped[row["section"]][row["table"]].append(row)

        lines.extend(["", f"## {schema_title}", ""])
        for section in sorted(grouped, key=lambda name: section_order.get(name, 999)):
            lines.extend(["", f"### {section}", ""])
            note = section_notes.get(section)
            if note:
                lines.extend([note, ""])

            for table_name in sorted(grouped[section]):
                lines.extend([f"#### `{table_name}`", ""])
                lines.extend(make_table(grouped[section][table_name]))
                lines.append("")

    OUTPUT_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
