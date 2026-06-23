import React from "react";
import { createRoot } from "react-dom/client";
import {
  Bell,
  CalendarDays,
  FileChartColumnIncreasing,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  CircleDollarSign,
  Database,
  Download,
  ExternalLink,
  FileText,
  Files,
  FolderSearch,
  Mic,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Presentation,
  RefreshCw,
  Search,
  Send,
  Settings,
  ShieldCheck,
  Sparkles,
  Hourglass,
  X,
  Zap
} from "lucide-react";
import type {
  Artifact,
  BootstrapResponse,
  ChatHistoryItem,
  ChatMessage,
  DataAvailabilityResponse,
  DataAvailabilityRow,
  DataSourceRegistryResponse,
  DocumentSearchResponse,
  DocumentSummary,
  Filters,
  HtmlWidget,
  PreviewTarget,
  ReleaseCalendarResponse,
  ReleaseEvent,
  ReportSearchResponse,
  ReportSubscriptionResponse,
  ReportSummary,
  V2Event,
  FinalResponseShell
} from "./types";
import "./styles.css";

const SOURCE_OPTIONS = [
  ["supplementary_financials", "Supplementary Financials", CircleDollarSign],
  ["investor_slides", "Investor Slides", Presentation],
  ["rts", "Reports to Shareholders", FileText],
  ["pillar3", "Pillar 3", ShieldCheck],
  ["transcripts", "Transcripts", Mic],
  ["event_transcripts", "Event Transcripts", CalendarDays]
] as const;

const DEFAULT_SOURCE_IDS = SOURCE_OPTIONS.map(([sourceId]) => sourceId);

interface SourceOption {
  id: string;
  label: string;
  description: string;
}

const FALLBACK_SOURCE_OPTIONS: SourceOption[] = SOURCE_OPTIONS.map(([id, label]) => ({
  id,
  label,
  description: ""
}));

const DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001";

const emptyFilters: Filters = {
  source_ids: [...DEFAULT_SOURCE_IDS],
  bank_symbols: [],
  bank_categories: [],
  fiscal_years: [],
  quarters: [],
  keyword: ""
};

type DrawerAppKey =
  | "coverage"
  | "files"
  | "releaseCalendar"
  | "reportDownloader"
  | "reportScheduler"
  | "preview"
  | "artifacts";

type ExpandedApps = Record<DrawerAppKey, boolean>;

type ChatStreamItem =
  | { type: "message"; message: ChatMessage }
  | { type: "widget"; widget: HtmlWidget }
  | { type: "thinking"; id: string; prompt: string };

interface SendMessageOptions {
  queryContent?: string;
  replaceWidget?: {
    widgetId: string;
    assistantMessage: ChatMessage;
  };
}

type ModelMode = "large" | "small";
type SearchMode = "quick" | "deep";

interface CalendarFilters {
  category: string;
  bank: string;
  year: string;
  quarter: string;
  month: string;
  selectedDay: string;
}

interface SchedulerScope {
  scopeType: string;
  category: string;
  bank: string;
}

function currentMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function defaultCalendarFilters(): CalendarFilters {
  return {
    category: "",
    bank: "",
    year: "",
    quarter: "",
    month: currentMonth(),
    selectedDay: ""
  };
}

function defaultSchedulerScope(): SchedulerScope {
  return {
    scopeType: "all_banks",
    category: "",
    bank: ""
  };
}

function buildQuery(filters: Filters): string {
  const params = new URLSearchParams();
  filters.source_ids.forEach((value) => params.append("sources", value));
  filters.bank_symbols.forEach((value) => params.append("banks", value));
  filters.bank_categories.forEach((value) => params.append("bank_categories", value));
  filters.fiscal_years.forEach((value) => params.append("years", String(value)));
  filters.quarters.forEach((value) => params.append("quarters", value));
  return params.toString();
}

function buildQueryContext(filters: Filters) {
  return {
    sources: filters.source_ids.map(sourceLabel),
    bank_categories: filters.bank_categories,
    bank_symbols: filters.bank_symbols,
    fiscal_years: filters.fiscal_years,
    quarters: filters.quarters
  };
}

function wsUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/v2/ws`;
}

function asAvailabilityResponse(value: unknown): DataAvailabilityResponse | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as DataAvailabilityResponse;
  return Array.isArray(candidate.rows) ? candidate : null;
}

function asReleaseCalendarResponse(value: unknown): ReleaseCalendarResponse | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as ReleaseCalendarResponse;
  return Array.isArray(candidate.events) ? candidate : null;
}

function sourceLabel(sourceId: string): string {
  return SOURCE_OPTIONS.find(([id]) => id === sourceId)?.[1] ?? sourceId;
}

function sourceShortLabel(sourceId: string): string {
  const labels: Record<string, string> = {
    supplementary_financials: "Supp",
    investor_slides: "Slides",
    rts: "RTS",
    pillar3: "P3",
    transcripts: "Call",
    event_transcripts: "Event"
  };
  return labels[sourceId] ?? sourceId;
}

function SourceIcon({ sourceId, size = 15 }: { sourceId: string; size?: number }) {
  const Icon = SOURCE_OPTIONS.find(([id]) => id === sourceId)?.[2] ?? FileText;
  return <Icon size={size} aria-hidden="true" />;
}

function uniqueSorted(values: Array<string | number | null | undefined>): string[] {
  return [...new Set(values.filter((value) => value !== null && value !== undefined).map(String))].sort();
}

function activeFilterCount(filters: Filters): number {
  return (isDefaultSourceSelection(filters.source_ids) ? 0 : 1)
    + filters.bank_symbols.length
    + filters.bank_categories.length
    + filters.fiscal_years.length
    + filters.quarters.length
    + (filters.keyword.trim() ? 1 : 0);
}

function isDefaultSourceSelection(sourceIds: string[]): boolean {
  return sourceIds.length === DEFAULT_SOURCE_IDS.length
    && DEFAULT_SOURCE_IDS.every((sourceId) => sourceIds.includes(sourceId));
}

function buildPayloadFilters(filters: Filters): Partial<Filters> | undefined {
  const payload: Partial<Filters> = {};
  if (!isDefaultSourceSelection(filters.source_ids)) {
    payload.source_ids = filters.source_ids;
  }
  if (filters.keyword.trim()) {
    payload.keyword = filters.keyword.trim();
  }
  return Object.keys(payload).length ? payload : undefined;
}

function buildOptionalContextPayload(filters: Filters): Partial<Filters> | undefined {
  const payload: Partial<Filters> = {};
  if (filters.bank_categories.length) payload.bank_categories = filters.bank_categories;
  if (filters.bank_symbols.length) payload.bank_symbols = filters.bank_symbols;
  if (filters.fiscal_years.length) payload.fiscal_years = filters.fiscal_years;
  if (filters.quarters.length) payload.quarters = filters.quarters;
  return Object.keys(payload).length ? payload : undefined;
}

function withoutThinking(items: ChatStreamItem[]): ChatStreamItem[] {
  return items.filter((item) => item.type !== "thinking");
}

function parseFinalShellContent(content: string): { content: string; finalShell: FinalResponseShell | null } {
  const open = "<aegis_final_shell>";
  const close = "</aegis_final_shell>";
  const start = content.indexOf(open);
  const end = content.indexOf(close);
  if (start === -1 || end === -1 || end < start) {
    return { content, finalShell: null };
  }
  const jsonText = content.slice(start + open.length, end).trim();
  const body = `${content.slice(0, start)}${content.slice(end + close.length)}`.trim();
  try {
    return {
      content: body,
      finalShell: JSON.parse(jsonText) as FinalResponseShell
    };
  } catch {
    return { content: body || content, finalShell: null };
  }
}

function parseWidgetContent(content: string): HtmlWidget | null {
  const open = "<aegis_widget>";
  const close = "</aegis_widget>";
  const start = content.indexOf(open);
  const end = content.indexOf(close);
  if (start === -1 || end === -1 || end < start) return null;
  const jsonText = content.slice(start + open.length, end).trim();
  try {
    const parsed = JSON.parse(jsonText) as HtmlWidget;
    return parsed && parsed.id && parsed.kind ? parsed : null;
  } catch {
    return null;
  }
}

function hydrateChatMessage(message: ChatMessage): ChatMessage {
  if (message.role !== "assistant") return message;
  const parsed = parseFinalShellContent(message.content);
  return {
    ...message,
    content: parsed.content,
    finalShell: message.finalShell ?? parsed.finalShell
  };
}

function hydrateChatStreamItem(message: ChatMessage): ChatStreamItem | null {
  const widget = parseWidgetContent(message.content);
  if (widget) return { type: "widget", widget };
  if (message.role === "tool") return null;
  return { type: "message", message: hydrateChatMessage(message) };
}

function hydrateChatHistoryItem(item: ChatHistoryItem): ChatStreamItem | null {
  if (item.type === "widget") return { type: "widget", widget: item.widget };
  return hydrateChatStreamItem(item.message);
}

function stripEvidenceMarkers(value: string | null | undefined): string {
  return String(value ?? "").replace(/\[\[[^\]]+\]\]/g, "").replace(/\s+/g, " ").trim();
}

function isSafeMarkdownHref(href: string): boolean {
  const trimmed = href.trim();
  if (!trimmed) return false;
  if (trimmed.startsWith("#") || trimmed.startsWith("/")) return true;
  try {
    const url = new URL(trimmed, window.location.href);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function renderInlineMarkdown(value: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const pattern = /(\[\[[^\]]+\]\]|\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(value)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(value.slice(lastIndex, match.index));
    }

    const token = match[0];
    const key = `${keyPrefix}-${nodes.length}`;
    if (token.startsWith("[[")) {
      nodes.push(
        <span className="citation-chip" key={key}>
          {token.slice(2, -2)}
        </span>
      );
    } else if (match[2] !== undefined && match[3] !== undefined) {
      const href = match[3].trim();
      nodes.push(
        isSafeMarkdownHref(href) ? (
          <a href={href} target="_blank" rel="noopener noreferrer" key={key}>
            {match[2]}
          </a>
        ) : (
          match[2]
        )
      );
    } else if (match[4] !== undefined) {
      nodes.push(<code key={key}>{match[4]}</code>);
    } else if (match[5] !== undefined) {
      nodes.push(<strong key={key}>{match[5]}</strong>);
    } else if (match[6] !== undefined) {
      nodes.push(<em key={key}>{match[6]}</em>);
    }
    lastIndex = pattern.lastIndex;
  }

  if (lastIndex < value.length) {
    nodes.push(value.slice(lastIndex));
  }
  return nodes;
}

function renderInlineMarkdownWithBreaks(value: string, keyPrefix: string): React.ReactNode[] {
  return value.split("\n").flatMap((line, index) => {
    const nodes = renderInlineMarkdown(line, `${keyPrefix}-line-${index}`);
    return index === 0 ? nodes : [<br key={`${keyPrefix}-br-${index}`} />, ...nodes];
  });
}

function splitMarkdownTableRow(line: string): string[] {
  const trimmed = line.trim();
  const bounded = trimmed.startsWith("|") && trimmed.endsWith("|") ? trimmed.slice(1, -1) : trimmed;
  return bounded.split("|").map((cell) => cell.trim());
}

function isMarkdownTableDivider(line: string): boolean {
  const cells = splitMarkdownTableRow(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function isMarkdownTableStart(lines: string[], index: number): boolean {
  const line = String(lines[index] ?? "").trim();
  const next = String(lines[index + 1] ?? "").trim();
  return line.includes("|") && isMarkdownTableDivider(next);
}

function renderMarkdownTable(lines: string[], startIndex: number, key: string): { node: React.ReactNode; nextIndex: number } {
  const headers = splitMarkdownTableRow(lines[startIndex]);
  const rows: string[][] = [];
  let index = startIndex + 2;

  while (index < lines.length && String(lines[index] ?? "").trim().includes("|")) {
    const rawLine = String(lines[index] ?? "").trim();
    if (!rawLine || isMarkdownTableDivider(rawLine)) break;
    rows.push(splitMarkdownTableRow(rawLine));
    index += 1;
  }

  return {
    node: (
      <div className="markdown-table-wrap" key={key}>
        <table className="markdown-table">
          <thead>
            <tr>
              {headers.map((header, cellIndex) => (
                <th key={`${key}-head-${cellIndex}`}>{renderInlineMarkdown(header, `${key}-head-${cellIndex}`)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={`${key}-row-${rowIndex}`}>
                {headers.map((_, cellIndex) => (
                  <td key={`${key}-cell-${rowIndex}-${cellIndex}`}>
                    {renderInlineMarkdown(row[cellIndex] ?? "", `${key}-cell-${rowIndex}-${cellIndex}`)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    ),
    nextIndex: index
  };
}

function markdownBlockBoundary(line: string, lines: string[], index: number): boolean {
  const trimmed = line.trim();
  return (
    !trimmed ||
    trimmed.startsWith("```") ||
    /^#{1,3}\s+/.test(trimmed) ||
    /^\s*[-*]\s+/.test(line) ||
    /^\s*\d+[.)]\s+/.test(line) ||
    /^>\s?/.test(line) ||
    isMarkdownTableStart(lines, index)
  );
}

function renderMarkdownBlocks(content: string): React.ReactNode[] {
  const normalized = content.replace(/\n{3,}/g, "\n\n").trim();
  if (!normalized) return [];

  const lines = normalized.split(/\r?\n/);
  const blocks: React.ReactNode[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = String(lines[index] ?? "");
    const trimmed = line.trim();
    const key = `md-${blocks.length}`;

    if (!trimmed) {
      index += 1;
      continue;
    }

    if (trimmed.startsWith("```")) {
      const language = trimmed.slice(3).trim();
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !String(lines[index] ?? "").trim().startsWith("```")) {
        codeLines.push(String(lines[index] ?? ""));
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(
        <pre key={key}>
          <code className={language ? `language-${language}` : undefined}>{codeLines.join("\n")}</code>
        </pre>
      );
      continue;
    }

    if (isMarkdownTableStart(lines, index)) {
      const table = renderMarkdownTable(lines, index, key);
      blocks.push(table.node);
      index = table.nextIndex;
      continue;
    }

    const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const children = renderInlineMarkdown(heading[2], `${key}-heading`);
      if (level === 1) {
        blocks.push(<h1 key={key}>{children}</h1>);
      } else if (level === 2) {
        blocks.push(<h2 key={key}>{children}</h2>);
      } else {
        blocks.push(<h3 key={key}>{children}</h3>);
      }
      index += 1;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length) {
        const item = String(lines[index] ?? "").match(/^\s*[-*]\s+(.+)$/);
        if (!item) break;
        items.push(item[1]);
        index += 1;
      }
      blocks.push(
        <ul key={key}>
          {items.map((item, itemIndex) => (
            <li key={`${key}-item-${itemIndex}`}>{renderInlineMarkdown(item, `${key}-item-${itemIndex}`)}</li>
          ))}
        </ul>
      );
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length) {
        const item = String(lines[index] ?? "").match(/^\s*\d+[.)]\s+(.+)$/);
        if (!item) break;
        items.push(item[1]);
        index += 1;
      }
      blocks.push(
        <ol key={key}>
          {items.map((item, itemIndex) => (
            <li key={`${key}-item-${itemIndex}`}>{renderInlineMarkdown(item, `${key}-item-${itemIndex}`)}</li>
          ))}
        </ol>
      );
      continue;
    }

    if (/^>\s?/.test(line)) {
      const quoteLines: string[] = [];
      while (index < lines.length) {
        const quote = String(lines[index] ?? "").match(/^>\s?(.*)$/);
        if (!quote) break;
        quoteLines.push(quote[1]);
        index += 1;
      }
      blocks.push(<blockquote key={key}>{renderInlineMarkdownWithBreaks(quoteLines.join("\n"), `${key}-quote`)}</blockquote>);
      continue;
    }

    const paragraph = [trimmed];
    index += 1;
    while (index < lines.length && !markdownBlockBoundary(String(lines[index] ?? ""), lines, index)) {
      paragraph.push(String(lines[index] ?? "").trim());
      index += 1;
    }
    blocks.push(<p key={key}>{renderInlineMarkdownWithBreaks(paragraph.join("\n"), `${key}-paragraph`)}</p>);
  }

  return blocks;
}

function MarkdownContent({
  content,
  className = "",
  drafting = false
}: {
  content: string;
  className?: string;
  drafting?: boolean;
}) {
  const blocks = renderMarkdownBlocks(content);
  if (blocks.length === 0 && !drafting) return null;
  return <div className={`markdown-content ${className} ${drafting ? "drafting" : ""}`.trim()}>{blocks}</div>;
}

function textFromTrustedHtml(html: string): string {
  const template = document.createElement("template");
  template.innerHTML = html;
  return (template.content.querySelector("p")?.textContent ?? template.content.textContent ?? "").trim();
}

function clarificationQuestion(widget: HtmlWidget): string {
  const dataQuestion = widget.data.question;
  if (typeof dataQuestion === "string" && dataQuestion.trim()) {
    return dataQuestion.trim();
  }
  return textFromTrustedHtml(widget.html) || widget.title;
}

function quarterRank(quarter: string): number {
  const match = quarter.match(/\d+/);
  return match ? Number(match[0]) : 0;
}

function availabilityPeriodLabel(row: DataAvailabilityRow): string {
  return `${row.quarter} ${row.fiscal_year}`;
}

function sortedAvailabilityRows(rows: DataAvailabilityRow[]): DataAvailabilityRow[] {
  return [...rows].sort((a, b) => {
    const yearDelta = b.fiscal_year - a.fiscal_year;
    if (yearDelta !== 0) return yearDelta;
    const quarterDelta = quarterRank(b.quarter) - quarterRank(a.quarter);
    if (quarterDelta !== 0) return quarterDelta;
    const categoryDelta = a.bank_category.localeCompare(b.bank_category);
    if (categoryDelta !== 0) return categoryDelta;
    return a.bank_symbol.localeCompare(b.bank_symbol);
  });
}

function availabilityDisplayRows(rows: DataAvailabilityRow[], limit: number) {
  const sortedRows = sortedAvailabilityRows(rows).slice(0, limit);
  const spanLength = (startIndex: number, matcher: (row: DataAvailabilityRow) => boolean): number => {
    let endIndex = startIndex;
    while (endIndex < sortedRows.length && matcher(sortedRows[endIndex])) {
      endIndex += 1;
    }
    return endIndex - startIndex;
  };

  return sortedRows.map((row, index) => {
    const previous = sortedRows[index - 1];
    const period = availabilityPeriodLabel(row);
    const previousPeriod = previous ? availabilityPeriodLabel(previous) : "";
    const samePeriod = Boolean(previous && previousPeriod === period);
    const sameCategory = samePeriod && previous?.bank_category === row.bank_category;
    const sameBank = sameCategory && previous?.bank_symbol === row.bank_symbol;
    const periodRowSpan = samePeriod
      ? 0
      : spanLength(index, (candidate) => availabilityPeriodLabel(candidate) === period);
    const categoryRowSpan = sameCategory
      ? 0
      : spanLength(
          index,
          (candidate) => availabilityPeriodLabel(candidate) === period && candidate.bank_category === row.bank_category
        );
    const bankRowSpan = sameBank
      ? 0
      : spanLength(
          index,
          (candidate) =>
            availabilityPeriodLabel(candidate) === period &&
            candidate.bank_category === row.bank_category &&
            candidate.bank_symbol === row.bank_symbol
        );
    return {
      row,
      startsPeriod: index > 0 && !samePeriod,
      startsCategory: index > 0 && samePeriod && !sameCategory,
      period,
      periodRowSpan,
      category: row.bank_category,
      categoryRowSpan,
      bank: row.bank_symbol,
      bankRowSpan
    };
  });
}

function artifactHtml(title: string, summary: string, points: string[]): string {
  return `
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>${title}</title>
        <style>
          body{font-family:Inter,Arial,sans-serif;margin:28px;color:#182230;line-height:1.45}
          h1{font-size:22px;margin:0 0 8px}
          p{font-size:13px;color:#4f5f72}
          li{margin:7px 0;font-size:13px}
          .tag{display:inline-block;margin-bottom:12px;padding:4px 7px;background:#e8f5f8;color:#084f6b;font-size:11px;font-weight:700}
        </style>
      </head>
      <body>
        <span class="tag">Aegis session artifact</span>
        <h1>${title}</h1>
        <p>${summary}</p>
        <ul>
          ${points.map((point) => `<li>${point}</li>`).join("")}
        </ul>
      </body>
    </html>
  `;
}

function exampleArtifacts(): Artifact[] {
  const now = Date.now();
  const createdAt = (offsetMinutes: number) => new Date(now - offsetMinutes * 60_000).toISOString();
  return [
    {
      id: "mock-research-revenue-trend",
      session_id: "mock-session",
      kind: "quick_search",
      title: "Revenue Trend Research",
      html: artifactHtml(
        "Revenue Trend Research",
        "Aegis compared Q1 2026 revenue commentary and supplementary financials across the Canadian bank peer set.",
        [
          "Revenue was broadly firmer year over year, with segment mix driving the largest differences.",
          "Capital markets and wealth results contributed to upside in several banks.",
          "Retail banking trends were more mixed and need source-level follow-up before final wording."
        ]
      ),
      source_widget_ids: ["example-html-widget"],
      evidence_ids: ["mock-evidence-ry-revenue", "mock-evidence-td-revenue"],
      created_at: createdAt(0)
    },
    {
      id: "mock-report-canadian-revenue",
      session_id: "mock-session",
      kind: "report",
      title: "Canadian Banks Revenue Report",
      html: artifactHtml(
        "Canadian Banks Revenue Report",
        "Generated HTML report summarizing revenue movement, peer differences, and source-backed caveats for Q1 2026.",
        [
          "Topline revenue increased across the covered Canadian bank group.",
          "The report separates reported revenue movement from segment-level drivers.",
          "Recommended next step: add charts for net interest income, non-interest income, and trading revenue."
        ]
      ),
      source_widget_ids: ["example-status-widget", "example-html-widget"],
      evidence_ids: ["mock-evidence-revenue-table"],
      created_at: createdAt(4)
    },
    {
      id: "mock-research-capital-credit",
      session_id: "mock-session",
      kind: "deep_search",
      title: "Capital And Credit Notes",
      html: artifactHtml(
        "Capital And Credit Notes",
        "Aegis captured early observations on CET1 movement, provision commentary, and credit quality changes.",
        [
          "Capital ratios remained within management target ranges in the mock source set.",
          "Credit commentary focused on normalization rather than broad deterioration.",
          "Provision trends should be reconciled against supplementary financial schedules before publishing."
        ]
      ),
      source_widget_ids: [],
      evidence_ids: ["mock-evidence-cet1", "mock-evidence-pcl"],
      created_at: createdAt(8)
    },
    {
      id: "mock-report-peer-summary",
      session_id: "mock-session",
      kind: "report",
      title: "Peer Summary Brief",
      html: artifactHtml(
        "Peer Summary Brief",
        "A concise generated report that rolls up revenue, capital, credit, and release-timing observations.",
        [
          "Peer comparison highlights are grouped by theme and fiscal period.",
          "The artifact is intended for preview first, then download or synthesis into a final report.",
          "Open questions are carried forward as follow-up research tasks."
        ]
      ),
      source_widget_ids: [],
      evidence_ids: ["mock-evidence-peer-summary"],
      created_at: createdAt(12)
    },
    {
      id: "mock-research-source-coverage",
      session_id: "mock-session",
      kind: "quick_search",
      title: "Source Coverage Check",
      html: artifactHtml(
        "Source Coverage Check",
        "Aegis checked which documents were available before running the revenue trend analysis.",
        [
          "Supplementary financials and investor slides were available for the selected Q1 2026 context.",
          "Missing documents should be flagged before relying on peer comparisons.",
          "This research artifact can support a later report synthesis step."
        ]
      ),
      source_widget_ids: ["example-status-widget"],
      evidence_ids: ["mock-evidence-coverage"],
      created_at: createdAt(16)
    }
  ];
}

function App() {
  const [filters, setFilters] = React.useState<Filters>(emptyFilters);
  const [sourceOptions, setSourceOptions] = React.useState<SourceOption[]>(FALLBACK_SOURCE_OPTIONS);
  const [availability, setAvailability] = React.useState<DataAvailabilityResponse | null>(null);
  const [availabilityCatalog, setAvailabilityCatalog] = React.useState<DataAvailabilityResponse | null>(null);
  const [documents, setDocuments] = React.useState<DocumentSummary[]>([]);
  const [releaseCalendar, setReleaseCalendar] = React.useState<ReleaseCalendarResponse | null>(null);
  const [reports, setReports] = React.useState<ReportSearchResponse | null>(null);
  const [subscriptions, setSubscriptions] = React.useState<ReportSubscriptionResponse | null>(null);
  const [artifacts, setArtifacts] = React.useState<Artifact[]>([]);
  const [chatItems, setChatItems] = React.useState<ChatStreamItem[]>([]);
  const [runtimeUserId, setRuntimeUserId] = React.useState(DEFAULT_USER_ID);
  const [activeConversationId, setActiveConversationId] = React.useState<string | null>(null);
  const [preview, setPreview] = React.useState<PreviewTarget>({ kind: "empty", title: "Preview" });
  const [input, setInput] = React.useState("");
  const [modelMode, setModelMode] = React.useState<ModelMode>("small");
  const [searchMode, setSearchMode] = React.useState<SearchMode>("quick");
  const [status, setStatus] = React.useState("Connecting");
  const [queryFiltersOpen, setQueryFiltersOpen] = React.useState(false);
  const [rightDrawerOpen, setRightDrawerOpen] = React.useState(false);
  const [expandedApps, setExpandedApps] = React.useState<ExpandedApps>({
    coverage: false,
    files: false,
    releaseCalendar: false,
    reportDownloader: false,
    reportScheduler: false,
    preview: true,
    artifacts: true
  });
  const [openTreeKeys, setOpenTreeKeys] = React.useState<Set<string>>(new Set());
  const [calendarFilters, setCalendarFilters] = React.useState<CalendarFilters>(defaultCalendarFilters);
  const [schedulerScope, setSchedulerScope] = React.useState<SchedulerScope>(defaultSchedulerScope);
  const socketRef = React.useRef<WebSocket | null>(null);
  const inputRef = React.useRef<HTMLTextAreaElement | null>(null);
  const messageStreamRef = React.useRef<HTMLDivElement | null>(null);
  const ignoredConversationIdsRef = React.useRef<Set<string>>(new Set());

  const catalogRows = availabilityCatalog?.rows ?? availability?.rows ?? [];
  const categoryOptions = uniqueSorted(catalogRows.map((row) => row.bank_category));
  const selectedCategory = filters.bank_categories[0] ?? "";
  const bankRows = selectedCategory
    ? catalogRows.filter((row) => row.bank_category === selectedCategory)
    : catalogRows;
  const bankOptions = [...new Map(bankRows.map((row) => [row.bank_symbol, row.bank_name])).entries()]
    .sort(([a], [b]) => a.localeCompare(b));
  const selectedBank = filters.bank_symbols[0] ?? "";
  const periodRows = selectedBank ? bankRows.filter((row) => row.bank_symbol === selectedBank) : bankRows;
  const yearOptions = uniqueSorted(periodRows.map((row) => row.fiscal_year)).sort((a, b) => Number(b) - Number(a));
  const selectedYear = filters.fiscal_years[0] ? String(filters.fiscal_years[0]) : "";
  const quarterRows = selectedYear ? periodRows.filter((row) => String(row.fiscal_year) === selectedYear) : periodRows;
  const quarterOptions = uniqueSorted(quarterRows.map((row) => row.quarter));

  const resizeInput = React.useCallback(() => {
    const element = inputRef.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${element.scrollHeight}px`;
  }, []);

  React.useLayoutEffect(() => {
    resizeInput();
  }, [input, resizeInput]);

  React.useLayoutEffect(() => {
    const stream = messageStreamRef.current;
    if (!stream) return;
    stream.scrollTop = stream.scrollHeight;
  }, [chatItems]);

  const loadAvailability = React.useCallback(async (nextFilters = filters) => {
    const query = buildQuery(nextFilters);
    const response = await fetch(`/api/v2/availability${query ? `?${query}` : ""}`);
    if (!response.ok) throw new Error(await response.text());
    const data = (await response.json()) as DataAvailabilityResponse;
    setAvailability(data);
  }, [filters]);

  const loadAvailabilityCatalog = React.useCallback(async () => {
    const response = await fetch("/api/v2/optional-context?limit=2000");
    if (!response.ok) throw new Error(await response.text());
    setAvailabilityCatalog((await response.json()) as DataAvailabilityResponse);
  }, []);

  const loadDataSources = React.useCallback(async () => {
    const response = await fetch("/api/v2/data-sources");
    if (!response.ok) throw new Error(await response.text());
    const data = (await response.json()) as DataSourceRegistryResponse;
    const nextOptions = data.data_sources.map((source) => ({
      id: source.data_source_name,
      label: source.data_source_display_name,
      description: source.data_source_description
    }));
    if (nextOptions.length === 0) return;
    const nextSourceIds = nextOptions.map((source) => source.id);
    setSourceOptions(nextOptions);
    setFilters((current) => {
      const selectedSourceIds = current.source_ids.filter((sourceId) => nextSourceIds.includes(sourceId));
      const hasDefaultSelection =
        current.source_ids.length === DEFAULT_SOURCE_IDS.length &&
        DEFAULT_SOURCE_IDS.every((sourceId) => current.source_ids.includes(sourceId));
      return {
        ...current,
        source_ids: hasDefaultSelection || selectedSourceIds.length === 0 ? nextSourceIds : selectedSourceIds
      };
    });
  }, []);

  const loadRuntimeBootstrap = React.useCallback(async () => {
    const response = await fetch("/api/v2/bootstrap");
    if (!response.ok) throw new Error(await response.text());
    const data = (await response.json()) as BootstrapResponse;
    setRuntimeUserId(data.user_id);
    setActiveConversationId(data.active_conversation?.conversation_id ?? null);
    const historyItems = data.chat_items?.length
      ? data.chat_items.flatMap((item) => {
          const hydrated = hydrateChatHistoryItem(item);
          return hydrated ? [hydrated] : [];
        })
      : data.messages.flatMap((message) => {
          const item = hydrateChatStreamItem(message);
          return item ? [item] : [];
        });
    setChatItems(historyItems);
    setArtifacts(data.recent_artifacts);
  }, []);

  const loadDocuments = React.useCallback(async (nextFilters = filters) => {
    const query = buildQuery(nextFilters);
    const response = await fetch(`/api/v2/documents${query ? `?${query}` : ""}`);
    if (!response.ok) throw new Error(await response.text());
    const data = (await response.json()) as DocumentSearchResponse;
    setDocuments(data.documents);
  }, [filters]);

  const loadReleaseCalendar = React.useCallback(async (nextFilters = calendarFilters) => {
    const params = new URLSearchParams({ month: nextFilters.month });
    if (nextFilters.category) params.append("bank_categories", nextFilters.category);
    if (nextFilters.bank) params.append("banks", nextFilters.bank);
    if (nextFilters.year) params.append("years", nextFilters.year);
    if (nextFilters.quarter) params.append("quarters", nextFilters.quarter);
    const response = await fetch(`/api/v2/release-calendar?${params.toString()}`);
    if (!response.ok) throw new Error(await response.text());
    setReleaseCalendar((await response.json()) as ReleaseCalendarResponse);
  }, [calendarFilters]);

  const loadReports = React.useCallback(async () => {
    const response = await fetch("/api/v2/reports");
    if (!response.ok) throw new Error(await response.text());
    setReports((await response.json()) as ReportSearchResponse);
  }, []);

  const loadSubscriptions = React.useCallback(async () => {
    const response = await fetch("/api/v2/report-subscriptions");
    if (!response.ok) throw new Error(await response.text());
    setSubscriptions((await response.json()) as ReportSubscriptionResponse);
  }, []);

  React.useEffect(() => {
    loadDataSources().catch((error) => setStatus(`Source registry error: ${error.message}`));
    loadAvailabilityCatalog().catch((error) => setStatus(`Catalog error: ${error.message}`));
    loadRuntimeBootstrap().catch((error) => setStatus(`Runtime error: ${error.message}`));
  }, []);

  React.useEffect(() => {
    const socket = new WebSocket(wsUrl());
    socketRef.current = socket;
    socket.addEventListener("open", () => setStatus("Connected"));
    socket.addEventListener("close", () => setStatus("Disconnected"));
    socket.addEventListener("message", (raw) => {
      const event = JSON.parse(raw.data) as V2Event;
      handleEvent(event);
    });
    return () => socket.close();
  }, []);

  function handleEvent(event: V2Event) {
    const eventConversationId = typeof event.payload.conversation_id === "string"
      ? event.payload.conversation_id
      : "";
    if (eventConversationId && ignoredConversationIdsRef.current.has(eventConversationId)) {
      return;
    }
    if (eventConversationId) setActiveConversationId(eventConversationId);

    if (event.type === "session.ready") {
      setStatus("Ready");
      return;
    }
    if (event.type === "chat.message") {
      const payload = event.payload as { role?: ChatMessage["role"]; content?: string; stream_id?: string; message_id?: string };
      const message = hydrateChatMessage({
        id: payload.message_id ?? event.event_id,
        role: payload.role ?? "assistant",
        content: payload.content ?? "",
        streamId: payload.stream_id
      });
      setChatItems((current) => {
        const currentWithoutThinking = withoutThinking(current);
        if (message.streamId) {
          let replaced = false;
          const nextItems = currentWithoutThinking.map((item) => {
            if (item.type === "message" && item.message.streamId === message.streamId) {
              replaced = true;
              return { type: "message", message } as ChatStreamItem;
            }
            return item;
          });
          if (replaced) return nextItems;
        }
        return [...currentWithoutThinking, { type: "message", message }];
      });
      return;
    }
    if (event.type === "final_response.started") {
      const payload = event.payload as { stream_id?: string; shell?: FinalResponseShell };
      const streamId = payload.stream_id ?? event.event_id;
      setChatItems((current) => {
        const currentWithoutThinking = withoutThinking(current);
        if (currentWithoutThinking.some((item) => item.type === "message" && item.message.streamId === streamId)) {
          return currentWithoutThinking;
        }
        return [
          ...currentWithoutThinking,
          {
            type: "message",
            message: {
              id: `assistant-${streamId}`,
              role: "assistant",
              content: "",
              streamId,
              finalShell: payload.shell ?? null
            }
          }
        ];
      });
      return;
    }
    if (event.type === "chat.delta") {
      const payload = event.payload as { role?: ChatMessage["role"]; content?: string; stream_id?: string };
      const streamId = payload.stream_id ?? event.event_id;
      const delta = payload.content ?? "";
      if (!delta) return;
      setChatItems((current) => {
        const currentWithoutThinking = withoutThinking(current);
        let replaced = false;
        const nextItems = currentWithoutThinking.map((item) => {
          if (item.type === "message" && item.message.streamId === streamId) {
            replaced = true;
            return {
              type: "message",
              message: {
                ...item.message,
                content: `${item.message.content}${delta}`
              }
            } as ChatStreamItem;
          }
          return item;
        });
        if (replaced) return nextItems;
        return [
          ...nextItems,
          {
            type: "message",
            message: {
              id: `assistant-${streamId}`,
              role: payload.role ?? "assistant",
              content: delta,
              streamId
            }
          }
        ];
      });
      return;
    }
    if (event.type.startsWith("tool.")) {
      const payload = event.payload as { name?: string };
      setStatus(`${event.type.replace("tool.", "")}: ${payload.name ?? "tool"}`);
      setChatItems((current) => withoutThinking(current));
      return;
    }
    if (event.type.startsWith("widget.")) {
      const payload = event.payload as { widget?: HtmlWidget };
      if (!payload.widget) return;
      const widget = payload.widget as HtmlWidget;
      setChatItems((current) => {
        const currentWithoutThinking = withoutThinking(current);
        let replaced = false;
        const nextItems = currentWithoutThinking.map((item) => {
          if (item.type === "widget" && item.widget.id === widget.id) {
            replaced = true;
            return { type: "widget", widget } as ChatStreamItem;
          }
          return item;
        });
        return replaced ? nextItems : [...nextItems, { type: "widget", widget }];
      });
      const response = asAvailabilityResponse(payload.widget.data);
      if (response) setAvailability(response);
      return;
    }
    if (event.type === "artifact.created" || event.type === "artifact.updated") {
      const payload = event.payload as { artifact?: Artifact };
      if (!payload.artifact) return;
      setChatItems((current) => withoutThinking(current));
      setArtifacts((current) => {
        const others = current.filter((artifact) => artifact.id !== payload.artifact?.id);
        return [payload.artifact as Artifact, ...others];
      });
    }
  }

  function applyFilters(nextFilters: Filters) {
    setFilters(nextFilters);
    loadAvailability(nextFilters).catch((error) => setStatus(`Availability error: ${error.message}`));
    loadDocuments(nextFilters).catch((error) => setStatus(`Document error: ${error.message}`));
  }

  function updateCoverageFilters(patch: Partial<Filters>) {
    const next = { ...filters, ...patch };
    if (patch.bank_categories) next.bank_symbols = [];
    if (patch.bank_categories || patch.bank_symbols) {
      next.fiscal_years = [];
      next.quarters = [];
    }
    if (patch.fiscal_years) next.quarters = [];
    applyFilters(next);
  }

  function updateQueryFilters(patch: Partial<Filters>) {
    setFilters({ ...filters, ...patch });
  }

  function updateQueryContextFilters(patch: Partial<Filters>) {
    const next = { ...filters, ...patch };
    if (patch.bank_categories) next.bank_symbols = [];
    setFilters(next);
  }

  function resetCoverageFilters() {
    applyFilters({ ...emptyFilters });
  }

  function resetFileExplorer() {
    setOpenTreeKeys(new Set());
  }

  async function resetChatSession() {
    try {
      const response = await fetch(`/api/v2/conversations?user_id=${encodeURIComponent(runtimeUserId)}`, {
        method: "DELETE"
      });
      if (!response.ok) throw new Error(await response.text());
      ignoredConversationIdsRef.current.clear();
    } catch (error) {
      setStatus(`Chat reset error: ${(error as Error).message}`);
      return;
    }
    setActiveConversationId((current) => {
      return null;
    });
    setChatItems([]);
    setArtifacts([]);
    setPreview({ kind: "empty", title: "Preview" });
    setInput("");
    setQueryFiltersOpen(false);
    setStatus("Ready");
  }

  async function resolveClarificationWidget(widget: HtmlWidget, question: string): Promise<ChatMessage | null> {
    if (!activeConversationId) return null;
    const response = await fetch(
      `/api/v2/conversations/${encodeURIComponent(activeConversationId)}/clarifications/resolve?user_id=${encodeURIComponent(runtimeUserId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ widget_id: widget.id, question })
      }
    );
    if (!response.ok) throw new Error(await response.text());
    return (await response.json()) as ChatMessage;
  }

  function openDocumentsFromRow(row: DataAvailabilityRow) {
    applyFilters({
      ...filters,
      bank_symbols: [row.bank_symbol],
      fiscal_years: [row.fiscal_year],
      quarters: [row.quarter],
      source_ids: row.source_ids
    });
  }

  function openDocument(document: DocumentSummary) {
    setPreview({
      kind: "source_document",
      title: document.filename,
      url: document.preview_url,
      source_id: document.source_id,
      file_id: document.file_id,
      location: `${document.bank_symbol} ${document.quarter} ${document.fiscal_year}`
    });
    setRightDrawerOpen(true);
    setExpandedApps((current) => ({ ...current, preview: true }));
  }

  async function openArtifact(artifact: Artifact) {
    let previewArtifact = artifact;
    try {
      const response = await fetch(`/api/v2/artifacts/${encodeURIComponent(artifact.id)}`);
      if (response.ok) {
        previewArtifact = (await response.json()) as Artifact;
      }
    } catch (error) {
      setStatus(`Artifact preview error: ${(error as Error).message}`);
    }
    setPreview({
      kind: "artifact",
      title: previewArtifact.title,
      artifact_id: previewArtifact.id,
      artifactHtml: previewArtifact.html
    });
    setRightDrawerOpen(true);
    setExpandedApps((current) => ({ ...current, preview: true }));
  }

  function openReport(report: ReportSummary) {
    setPreview({
      kind: "report",
      title: report.title,
      url: report.preview_url,
      location: `${report.bank_symbol} ${report.quarter} ${report.fiscal_year}`
    });
    setRightDrawerOpen(true);
    setExpandedApps((current) => ({ ...current, preview: true }));
  }

  async function runWidgetAction(action: HtmlWidget["actions"][number], widget: HtmlWidget) {
    const payload = action.payload as Partial<Filters>;
    if (action.action_type === "filter_documents") {
      setFilters({
        ...filters,
        source_ids: (payload.source_ids as string[]) ?? filters.source_ids,
        bank_symbols: (payload.bank_symbols as string[]) ?? filters.bank_symbols,
        fiscal_years: (payload.fiscal_years as number[]) ?? filters.fiscal_years,
        quarters: (payload.quarters as string[]) ?? filters.quarters
      });
      return;
    }
    if (action.action_type !== "clarification_reply") return;
    const clarificationPayload = action.payload as {
      filters?: Partial<Filters>;
      resend_query?: string;
      reply?: string;
      question?: string;
    };
    const patch = clarificationPayload.filters ?? {};
    const nextFilters = {
      ...filters,
      source_ids: patch.source_ids ?? filters.source_ids,
      bank_symbols: patch.bank_symbols ?? filters.bank_symbols,
      bank_categories: patch.bank_categories ?? filters.bank_categories,
      fiscal_years: patch.fiscal_years ?? filters.fiscal_years,
      quarters: patch.quarters ?? filters.quarters,
      keyword: patch.keyword ?? filters.keyword
    };
    setFilters(nextFilters);
    const reply = (clarificationPayload.reply || action.label).trim();
    const question = (clarificationPayload.question || clarificationQuestion(widget)).trim();
    let assistantMessage: ChatMessage = {
      id: `assistant-${widget.id}`,
      role: "assistant",
      content: question
    };
    try {
      const persistedMessage = await resolveClarificationWidget(widget, question);
      if (persistedMessage) assistantMessage = persistedMessage;
    } catch (error) {
      setStatus(`Clarification update error: ${(error as Error).message}`);
    }
    const resendQuery = clarificationPayload.resend_query?.trim();
    const queryContent = resendQuery && resendQuery !== reply
      ? `${resendQuery}\n\nClarification answer: ${reply}`
      : reply;
    sendMessage(reply, nextFilters, {
      queryContent,
      replaceWidget: {
        widgetId: widget.id,
        assistantMessage
      }
    });
  }

  function sendMessage(message = input, activeFilters = filters, options: SendMessageOptions = {}) {
    const content = message.trim();
    if (!content) return;
    const queryContent = (options.queryContent ?? content).trim();
    if (!queryContent) return;
    const turnId = crypto.randomUUID();
    setChatItems((current) => {
      const replacement = options.replaceWidget;
      const nextItems = replacement
        ? current.map((item) => {
            if (item.type === "widget" && item.widget.id === replacement.widgetId) {
              return { type: "message", message: replacement.assistantMessage } as ChatStreamItem;
            }
            return item;
          })
        : current;
      return [
        ...nextItems,
        {
          type: "message",
          message: { id: `user-${turnId}`, role: "user", content }
        },
        {
          type: "thinking",
          id: `thinking-${turnId}`,
          prompt: content
        }
      ];
    });
    const preferences = {
      fast_mode: modelMode === "small",
      model_mode: modelMode,
      search_mode: searchMode,
      research_depth: searchMode === "quick" ? "short" : "long"
    };
    const payloadFilters = buildPayloadFilters(activeFilters);
    const optionalContext = buildOptionalContextPayload(activeFilters);
    const payload = JSON.stringify({
      type: "message",
      query: queryContent,
      content,
      user_id: runtimeUserId,
      conversation_id: activeConversationId,
      filters: payloadFilters,
      optional_context: optionalContext,
      model_selection: modelMode,
      search_selection: searchMode,
      preferences
    });
    socketRef.current?.send(payload);
    setInput("");
    setQueryFiltersOpen(false);
  }

  function handleComposerKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    sendMessage();
  }

  function toggleApp(app: DrawerAppKey) {
    setExpandedApps((current) => ({ ...current, [app]: !current[app] }));
  }

  function toggleRightDrawer() {
    setRightDrawerOpen((current) => !current);
  }

  function updateCalendar(nextPatch: Partial<CalendarFilters>) {
    const next = { ...calendarFilters, ...nextPatch };
    if (nextPatch.category) next.bank = "";
    if (nextPatch.month || nextPatch.category || nextPatch.bank || nextPatch.year || nextPatch.quarter) {
      next.selectedDay = "";
    }
    setCalendarFilters(next);
    loadReleaseCalendar(next).catch((error) => setStatus(`Calendar error: ${error.message}`));
  }

  function resetCalendarFilters() {
    const next = defaultCalendarFilters();
    setCalendarFilters(next);
    loadReleaseCalendar(next).catch((error) => setStatus(`Calendar error: ${error.message}`));
  }

  function resetSchedulerFilters() {
    setSchedulerScope(defaultSchedulerScope());
  }

  async function postReportSubscription(request: {
    report_type: string;
    scope_type: "all_banks" | "category" | "bank";
    bank_category: string | null;
    bank_symbol: string | null;
  }) {
    const response = await fetch("/api/v2/report-subscriptions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request)
    });
    if (!response.ok) throw new Error(await response.text());
    const nextSubscriptions = (await response.json()) as ReportSubscriptionResponse;
    setSubscriptions(nextSubscriptions);
    return nextSubscriptions;
  }

  async function subscribe(reportType: string) {
    await postReportSubscription({
      report_type: reportType,
      scope_type: schedulerScope.scopeType as "all_banks" | "category" | "bank",
      bank_category: schedulerScope.scopeType === "category" ? schedulerScope.category : null,
      bank_symbol: schedulerScope.scopeType === "bank" ? schedulerScope.bank : null
    });
  }

  async function subscribeReportSelection(
    reportType: string,
    selectedBankSymbols: string[],
    selectedCategory: string,
    visibleBankSymbols: string[]
  ) {
    const selectedSymbols = [...new Set(selectedBankSymbols)];
    if (selectedSymbols.length === 0) return;
    const categorySelectionComplete = Boolean(selectedCategory)
      && visibleBankSymbols.length > 0
      && selectedSymbols.length === visibleBankSymbols.length
      && visibleBankSymbols.every((symbol) => selectedSymbols.includes(symbol));

    if (categorySelectionComplete) {
      await postReportSubscription({
        report_type: reportType,
        scope_type: "category",
        bank_category: selectedCategory,
        bank_symbol: null
      });
      return;
    }

    for (const bankSymbol of selectedSymbols) {
      await postReportSubscription({
        report_type: reportType,
        scope_type: "bank",
        bank_category: null,
        bank_symbol: bankSymbol
      });
    }
  }

  const reportBankOptions = [...new Map(catalogRows.map((row) => [row.bank_symbol, row.bank_name])).entries()]
    .sort(([a], [b]) => a.localeCompare(b));

  const calendarCategory = calendarFilters.category;
  const calendarBankRows = calendarCategory
    ? catalogRows.filter((row) => row.bank_category === calendarCategory)
    : catalogRows;
  const calendarBankOptions = [...new Map(calendarBankRows.map((row) => [row.bank_symbol, row.bank_name])).entries()]
    .sort(([a], [b]) => a.localeCompare(b));
  const calendarSelectedBank = calendarFilters.bank;
  const calendarPeriodRows = calendarSelectedBank
    ? calendarBankRows.filter((row) => row.bank_symbol === calendarSelectedBank)
    : calendarBankRows;
  const calendarYearOptions = uniqueSorted(calendarPeriodRows.map((row) => row.fiscal_year)).sort((a, b) => Number(b) - Number(a));
  const calendarSelectedYear = calendarFilters.year;
  const calendarQuarterRows = calendarSelectedYear
    ? calendarPeriodRows.filter((row) => String(row.fiscal_year) === calendarSelectedYear)
    : calendarPeriodRows;
  const calendarQuarterOptions = uniqueSorted(calendarQuarterRows.map((row) => row.quarter));

  return (
    <main
      className="app-shell"
      data-right-drawer={rightDrawerOpen ? "open" : "closed"}
    >
      <section className="chat-panel">
        <div className="temp-chat-toolbar">
          <button
            type="button"
            className="icon-command temp-chat-reset-button"
            title="Start a new chat"
            aria-label="Start a new chat"
            onClick={resetChatSession}
          >
            <RefreshCw size={15} />
            <span>New chat</span>
          </button>
        </div>
        <div className="message-stream" ref={messageStreamRef}>
          {chatItems.map((item) => {
            if (item.type === "widget") {
              return <WidgetView key={item.widget.id} widget={item.widget} runWidgetAction={runWidgetAction} />;
            }
            if (item.type === "thinking") {
              return <ThinkingBox key={item.id} />;
            }
            const message = item.message;
            return <MessageBubble key={message.id} message={message} />;
          })}
        </div>
        <div className="composer-shell">
          {queryFiltersOpen && (
            <QueryFilterPanel
              filters={filters}
              sourceOptions={sourceOptions}
              updateFilters={updateQueryFilters}
            />
          )}
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              sendMessage();
            }}
          >
            <div className="composer-input-shell">
              <button
                type="button"
                title="Datasource filters"
                aria-label="Datasource filters"
                aria-pressed={queryFiltersOpen}
                className={`icon-command composer-side-command filter-command ${activeFilterCount(filters) ? "has-filters" : ""}`}
                onClick={() => setQueryFiltersOpen((current) => !current)}
              >
                <Database size={18} />
                <span className="command-label">FILTERS</span>
                {activeFilterCount(filters) > 0 && <span className="filter-count-badge">{activeFilterCount(filters)}</span>}
              </button>
              <div className="composer-input-main">
                <div className="composer-context-row">
                  <QueryContextControls
                    filters={filters}
                    categoryOptions={categoryOptions}
                    bankOptions={bankOptions}
                    yearOptions={yearOptions}
                    quarterOptions={quarterOptions}
                    updateFilters={updateQueryContextFilters}
                  />
                  <ModelModeControl value={modelMode} setValue={setModelMode} />
                  <ResearchModeControl value={searchMode} setValue={setSearchMode} />
                </div>
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={handleComposerKeyDown}
                  placeholder="Ask about banks, filings, metrics, or reports"
                  rows={1}
                  aria-label="Ask Aegis"
                />
              </div>
              <button type="submit" title="Ask" aria-label="Ask" className="icon-command composer-side-command primary">
                <Send size={18} />
                <span className="command-label">SEND</span>
              </button>
            </div>
          </form>
        </div>
      </section>

      <Drawer
        side="right"
        open={rightDrawerOpen}
        title="Viewer"
        collapsedLabel="Viewer"
        onToggle={toggleRightDrawer}
      >
        <DrawerApp title="Viewer" icon={<ExternalLink size={16} />} expanded hideHeader onToggle={() => toggleApp("preview")}>
          <PreviewPanel preview={preview} />
        </DrawerApp>
        <DrawerApp title="Artifacts" icon={<FileText size={16} />} expanded hideHeader onToggle={() => undefined}>
          <ArtifactPanel artifacts={artifacts} openArtifact={openArtifact} />
        </DrawerApp>
      </Drawer>
    </main>
  );
}

function Drawer({
  side,
  open,
  title,
  collapsedLabel,
  onToggle,
  children
}: {
  side: "left" | "right";
  open: boolean;
  title: string;
  collapsedLabel: string;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  const ToggleIcon = side === "left"
    ? open ? PanelLeftClose : PanelLeftOpen
    : open ? PanelRightClose : PanelRightOpen;

  return (
    <aside className={`app-drawer ${side} ${open ? "open" : "closed"}`} aria-label={title}>
      {!open && <div className="drawer-rail">
        <button
          type="button"
          className="icon-command drawer-toggle"
          title={`Open ${title}`}
          aria-label={`Open ${title}`}
          onClick={onToggle}
        >
          <ToggleIcon size={18} />
        </button>
        <span className="drawer-vertical-label">{collapsedLabel}</span>
      </div>}
      <div className="drawer-content" aria-hidden={!open}>
        <div className="drawer-titlebar">
          <strong>{title}</strong>
          <button
            type="button"
            className="icon-command drawer-titlebar-toggle"
            title={`Close ${title}`}
            aria-label={`Close ${title}`}
            onClick={onToggle}
          >
            <ToggleIcon size={18} />
          </button>
        </div>
        <div className="drawer-app-stack">{children}</div>
      </div>
    </aside>
  );
}

function DrawerApp({
  title,
  kicker,
  icon,
  expanded,
  onToggle,
  appClassName,
  headerAction,
  hideHeader = false,
  children
}: {
  title: string;
  kicker?: string;
  icon: React.ReactNode;
  expanded: boolean;
  onToggle: () => void;
  appClassName?: string;
  headerAction?: {
    label: string;
    onClick: () => void;
    icon?: React.ReactNode;
  };
  hideHeader?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className={`drawer-app ${hideHeader ? "headerless" : ""} ${appClassName ?? ""} ${expanded ? "expanded" : "collapsed"}`}>
      {!hideHeader && <div className="drawer-app-header">
        <button type="button" className="drawer-app-title-button" aria-expanded={expanded} onClick={onToggle}>
          {icon}
          <span>
            {kicker && <small>{kicker}</small>}
            <strong>{title}</strong>
          </span>
        </button>
        {expanded && headerAction && (
          <button
            type="button"
            className="icon-command drawer-app-header-action"
            title={headerAction.label}
            aria-label={headerAction.label}
            onClick={headerAction.onClick}
          >
            {headerAction.icon ?? <RefreshCw size={15} />}
          </button>
        )}
        <button
          type="button"
          className="icon-command drawer-app-collapse"
          title={expanded ? `Collapse ${title}` : `Expand ${title}`}
          aria-label={expanded ? `Collapse ${title}` : `Expand ${title}`}
          aria-expanded={expanded}
          onClick={onToggle}
        >
          {expanded ? <ChevronDown size={17} /> : <ChevronRight size={17} />}
        </button>
      </div>}
      {expanded && <div className="drawer-app-body">{children}</div>}
    </section>
  );
}

function QueryFilterPanel({
  filters,
  sourceOptions,
  updateFilters
}: {
  filters: Filters;
  sourceOptions: SourceOption[];
  updateFilters: (patch: Partial<Filters>) => void;
}) {
  const sourceIds = sourceOptions.map((source) => source.id);
  const allSelected = sourceIds.every((sourceId) => filters.source_ids.includes(sourceId));

  function toggleSourceFilter(value: string) {
    const current = filters.source_ids;
    updateFilters({
      source_ids: current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value]
    });
  }

  function toggleAllFilters() {
    updateFilters({
      source_ids: allSelected ? [] : sourceIds
    });
  }

  return (
    <div className="query-filter-popover">
      <div className="query-filter-titlebar">
        <strong>Datasources</strong>
        <button type="button" className="text-command" onClick={toggleAllFilters}>
          {allSelected ? "Deselect all" : "Select all"}
        </button>
      </div>
      <div className="query-filter-list">
        {sourceOptions.map((source) => (
          <button
            key={source.id}
            type="button"
            className={`query-filter-row ${filters.source_ids.includes(source.id) ? "selected" : ""}`}
            aria-pressed={filters.source_ids.includes(source.id)}
            title={source.description || source.label}
            onClick={() => toggleSourceFilter(source.id)}
          >
            <SourceIcon sourceId={source.id} size={15} />
            <span>{source.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function QueryContextControls({
  filters,
  categoryOptions,
  bankOptions,
  yearOptions,
  quarterOptions,
  updateFilters
}: {
  filters: Filters;
  categoryOptions: string[];
  bankOptions: Array<[string, string]>;
  yearOptions: string[];
  quarterOptions: string[];
  updateFilters: (patch: Partial<Filters>) => void;
}) {
  return (
    <fieldset className="query-context-fieldset">
      <legend>+ OPTIONAL CONTEXT</legend>
      <div className="query-context-controls">
        <select
          value={filters.bank_categories[0] ?? ""}
          aria-label="Query category context"
          onChange={(event) => updateFilters({ bank_categories: event.target.value ? [event.target.value] : [] })}
        >
          <option value="">Category</option>
          {categoryOptions.map((category) => <option key={category} value={category}>{category}</option>)}
        </select>
        <select
          value={filters.fiscal_years[0] ? String(filters.fiscal_years[0]) : ""}
          aria-label="Query year context"
          onChange={(event) => updateFilters({ fiscal_years: event.target.value ? [Number(event.target.value)] : [] })}
        >
          <option value="">Year</option>
          {yearOptions.map((year) => <option key={year} value={year}>{year}</option>)}
        </select>
        <select
          value={filters.quarters[0] ?? ""}
          aria-label="Query quarter context"
          onChange={(event) => updateFilters({ quarters: event.target.value ? [event.target.value] : [] })}
        >
          <option value="">Quarter</option>
          {quarterOptions.map((quarter) => <option key={quarter} value={quarter}>{quarter}</option>)}
        </select>
        <select
          value={filters.bank_symbols[0] ?? ""}
          aria-label="Query banks context"
          onChange={(event) => updateFilters({ bank_symbols: event.target.value ? [event.target.value] : [] })}
        >
          <option value="">Banks</option>
          {bankOptions.map(([symbol, name]) => <option key={symbol} value={symbol}>{symbol} - {name}</option>)}
        </select>
      </div>
    </fieldset>
  );
}

function ModelModeControl({
  value,
  setValue
}: {
  value: ModelMode;
  setValue: (value: ModelMode) => void;
}) {
  return (
    <fieldset className="composer-mode-fieldset model-mode-fieldset">
      <legend><Settings size={9} aria-hidden="true" />MODEL</legend>
      <div className="composer-option-group" role="group" aria-label="Model mode">
        <button
          type="button"
          className={value === "small" ? "selected" : ""}
          aria-label="Small model"
          aria-pressed={value === "small"}
          onClick={() => setValue("small")}
        >
          <Zap size={16} />
          <span>SMALL</span>
        </button>
        <button
          type="button"
          className={value === "large" ? "selected" : ""}
          aria-label="Large model"
          aria-pressed={value === "large"}
          onClick={() => setValue("large")}
        >
          <Hourglass size={16} />
          <span>LARGE</span>
        </button>
      </div>
    </fieldset>
  );
}

function ResearchModeControl({
  value,
  setValue
}: {
  value: SearchMode;
  setValue: (value: SearchMode) => void;
}) {
  return (
    <fieldset className="composer-mode-fieldset research-mode-fieldset">
      <legend><Search size={9} aria-hidden="true" />SEARCH</legend>
      <div className="composer-option-group" role="group" aria-label="Research length">
        <button
          type="button"
          className={value === "quick" ? "selected" : ""}
          aria-label="Quick search"
          aria-pressed={value === "quick"}
          onClick={() => setValue("quick")}
        >
          <FileText size={16} />
          <span>QUICK</span>
        </button>
        <button
          type="button"
          className={value === "deep" ? "selected" : ""}
          aria-label="Deep search"
          aria-pressed={value === "deep"}
          onClick={() => setValue("deep")}
        >
          <Files size={16} />
          <span>DEEP</span>
        </button>
      </div>
    </fieldset>
  );
}

function AvailabilityPanel({
  availability,
  filters,
  categoryOptions,
  bankOptions,
  yearOptions,
  quarterOptions,
  updateFilters,
  openDocumentsFromRow
}: {
  availability: DataAvailabilityResponse | null;
  filters: Filters;
  categoryOptions: string[];
  bankOptions: Array<[string, string]>;
  yearOptions: string[];
  quarterOptions: string[];
  updateFilters: (patch: Partial<Filters>) => void;
  openDocumentsFromRow: (row: DataAvailabilityRow) => void;
}) {
  const rows = availabilityDisplayRows(availability?.rows ?? [], 120);

  return (
    <div className="availability-pane">
      <div className="coverage-filter-grid">
        <select
          value={filters.bank_categories[0] ?? ""}
          onChange={(event) => updateFilters({ bank_categories: event.target.value ? [event.target.value] : [] })}
        >
          <option value="">Category</option>
          {categoryOptions.map((category) => <option key={category} value={category}>{category}</option>)}
        </select>
        <select
          value={filters.bank_symbols[0] ?? ""}
          onChange={(event) => updateFilters({ bank_symbols: event.target.value ? [event.target.value] : [] })}
        >
          <option value="">Bank</option>
          {bankOptions.map(([symbol, name]) => <option key={symbol} value={symbol}>{symbol} - {name}</option>)}
        </select>
        <select
          value={filters.fiscal_years[0] ? String(filters.fiscal_years[0]) : ""}
          onChange={(event) => updateFilters({ fiscal_years: event.target.value ? [Number(event.target.value)] : [] })}
        >
          <option value="">Year</option>
          {yearOptions.map((year) => <option key={year} value={year}>{year}</option>)}
        </select>
        <select
          value={filters.quarters[0] ?? ""}
          onChange={(event) => updateFilters({ quarters: event.target.value ? [event.target.value] : [] })}
        >
          <option value="">Quarter</option>
          {quarterOptions.map((quarter) => <option key={quarter} value={quarter}>{quarter}</option>)}
        </select>
      </div>
      <div className="coverage-table-wrap">
        <table className="coverage-table">
          <thead>
            <tr>
              <th>Period</th>
              <th>Category</th>
              <th>Bank</th>
              {SOURCE_OPTIONS.map(([id, label]) => (
                <th key={id} className="source-heading">
                  <span>{label}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((display) => (
              <tr
                key={`${display.row.bank_symbol}-${display.row.fiscal_year}-${display.row.quarter}`}
                className={[
                  display.startsPeriod ? "period-break" : "",
                  display.startsCategory ? "category-break" : ""
                ].filter(Boolean).join(" ")}
                onClick={() => openDocumentsFromRow(display.row)}
              >
                {display.periodRowSpan > 0 && (
                  <td rowSpan={display.periodRowSpan} className="merged-cell period-cell">{display.period}</td>
                )}
                {display.categoryRowSpan > 0 && (
                  <td rowSpan={display.categoryRowSpan} className="merged-cell category-cell">{display.category}</td>
                )}
                {display.bankRowSpan > 0 && (
                  <td rowSpan={display.bankRowSpan} className="merged-cell bank-cell"><strong>{display.bank}</strong></td>
                )}
                {SOURCE_OPTIONS.map(([id]) => (
                  <td key={id} className={display.row.source_ids.includes(id) ? "source-cell has-source" : "source-cell"}>
                    {display.row.source_ids.includes(id) ? <SourceIcon sourceId={id} size={14} /> : null}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function DocumentPanel({
  documents,
  openTreeKeys,
  setOpenTreeKeys,
  openDocument
}: {
  documents: DocumentSummary[];
  openTreeKeys: Set<string>;
  setOpenTreeKeys: React.Dispatch<React.SetStateAction<Set<string>>>;
  openDocument: (document: DocumentSummary) => void;
}) {
  function toggleTree(key: string) {
    setOpenTreeKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const grouped = groupDocuments(documents);
  return (
    <div className="document-pane">
      <div className="document-tree">
        {grouped.length === 0 && <div className="empty-list">No preview documents available.</div>}
        {grouped.map((source) => (
          <TreeGroup key={source.key} label={source.label} depth={0} isOpen={openTreeKeys.has(source.key)} onToggle={() => toggleTree(source.key)}>
            {source.periods.map((period) => (
              <TreeGroup key={period.key} label={period.label} depth={1} isOpen={openTreeKeys.has(period.key)} onToggle={() => toggleTree(period.key)}>
                {period.banks.map((bank) => (
                  <TreeGroup key={bank.key} label={bank.label} depth={2} isOpen={openTreeKeys.has(bank.key)} onToggle={() => toggleTree(bank.key)}>
                    {bank.documents.map((document) => (
                      <button
                        key={`${document.source_id}-${document.file_id}`}
                        type="button"
                        className="tree-document"
                        style={{ paddingLeft: 44 }}
                        onClick={() => openDocument(document)}
                      >
                        <FileText size={14} />
                        <span>{document.filename}</span>
                      </button>
                    ))}
                  </TreeGroup>
                ))}
              </TreeGroup>
            ))}
          </TreeGroup>
        ))}
      </div>
    </div>
  );
}

function TreeGroup({
  label,
  depth,
  isOpen,
  onToggle,
  children
}: {
  label: string;
  depth: number;
  isOpen: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="tree-group">
      <button type="button" className="tree-toggle" style={{ paddingLeft: 8 + depth * 14 }} onClick={onToggle}>
        {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>{label}</span>
      </button>
      {isOpen && <div className="tree-children">{children}</div>}
    </div>
  );
}

function groupDocuments(documents: DocumentSummary[]) {
  return SOURCE_OPTIONS.map(([sourceId, label]) => {
    const sourceDocuments = documents.filter((document) => document.source_id === sourceId);
    const periodLabels = uniqueSorted(sourceDocuments.map((document) => `${document.quarter} ${document.fiscal_year}`));
    return {
      key: `source:${sourceId}`,
      label,
      periods: periodLabels.map((periodLabel) => {
        const periodDocuments = sourceDocuments.filter((document) => `${document.quarter} ${document.fiscal_year}` === periodLabel);
        const bankLabels = uniqueSorted(periodDocuments.map((document) => document.bank_symbol));
        return {
          key: `source:${sourceId}:period:${periodLabel}`,
          label: periodLabel,
          banks: bankLabels.map((bankSymbol) => ({
            key: `source:${sourceId}:period:${periodLabel}:bank:${bankSymbol}`,
            label: bankSymbol,
            documents: periodDocuments.filter((document) => document.bank_symbol === bankSymbol)
          }))
        };
      })
    };
  }).filter((source) => source.periods.length > 0);
}

function ReleaseCalendarPanel({
  calendar,
  filters,
  categoryOptions,
  bankOptions,
  yearOptions,
  quarterOptions,
  updateCalendar
}: {
  calendar: ReleaseCalendarResponse | null;
  filters: CalendarFilters;
  categoryOptions: string[];
  bankOptions: Array<[string, string]>;
  yearOptions: string[];
  quarterOptions: string[];
  updateCalendar: (patch: Partial<CalendarFilters>) => void;
}) {
  const events = calendar?.events ?? [];
  const selectedEvents = filters.selectedDay
    ? events.filter((event) => event.event_date.slice(0, 10) === filters.selectedDay)
    : events;
  return (
    <div className="calendar-pane">
      <div className="calendar-filters">
        <select value={filters.category} onChange={(event) => updateCalendar({ category: event.target.value })}>
          <option value="">Category</option>
          {categoryOptions.map((category) => <option key={category} value={category}>{category}</option>)}
        </select>
        <select value={filters.bank} onChange={(event) => updateCalendar({ bank: event.target.value })}>
          <option value="">Bank</option>
          {bankOptions.map(([symbol, name]) => <option key={symbol} value={symbol}>{symbol} - {name}</option>)}
        </select>
        <select value={filters.year} onChange={(event) => updateCalendar({ year: event.target.value })}>
          <option value="">Year</option>
          {yearOptions.map((year) => <option key={year} value={year}>{year}</option>)}
        </select>
        <select value={filters.quarter} onChange={(event) => updateCalendar({ quarter: event.target.value })}>
          <option value="">Quarter</option>
          {quarterOptions.map((quarter) => <option key={quarter} value={quarter}>{quarter}</option>)}
        </select>
      </div>
      <MonthCalendar filters={filters} events={events} updateCalendar={updateCalendar} />
      <EventList events={selectedEvents} />
    </div>
  );
}

function MonthCalendar({
  filters,
  events,
  updateCalendar
}: {
  filters: CalendarFilters;
  events: ReleaseEvent[];
  updateCalendar: (patch: Partial<CalendarFilters>) => void;
}) {
  const [year, month] = filters.month.split("-").map(Number);
  const first = new Date(year, month - 1, 1);
  const daysInMonth = new Date(year, month, 0).getDate();
  const blanks = first.getDay();
  const cells = [...Array(blanks).fill(null), ...Array.from({ length: daysInMonth }, (_, index) => index + 1)];
  const eventDates = new Map<string, number>();
  events.forEach((event) => {
    const day = event.event_date.slice(0, 10);
    eventDates.set(day, (eventDates.get(day) ?? 0) + 1);
  });
  function shiftMonth(delta: number) {
    const date = new Date(year, month - 1 + delta, 1);
    updateCalendar({ month: `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}` });
  }
  return (
    <div className="month-calendar">
      <div className="month-header">
        <button type="button" className="icon-command" title="Previous month" onClick={() => shiftMonth(-1)}><ChevronLeft size={15} /></button>
        <strong>{first.toLocaleString(undefined, { month: "long", year: "numeric" })}</strong>
        <button type="button" className="icon-command" title="Next month" onClick={() => shiftMonth(1)}><ChevronRight size={15} /></button>
      </div>
      <div className="calendar-grid">
        {["S", "M", "T", "W", "T", "F", "S"].map((day, index) => <span key={`${day}-${index}`} className="weekday">{day}</span>)}
        {cells.map((day, index) => {
          if (!day) return <span key={`blank-${index}`} />;
          const iso = `${filters.month}-${String(day).padStart(2, "0")}`;
          const count = eventDates.get(iso) ?? 0;
          return (
            <button
              key={iso}
              type="button"
              className={`calendar-day ${count ? "has-events" : ""} ${filters.selectedDay === iso ? "selected" : ""}`}
              onClick={() => updateCalendar({ selectedDay: filters.selectedDay === iso ? "" : iso })}
            >
              <span>{day}</span>
              {count > 0 && <small>{count}</small>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function EventList({ events }: { events: ReleaseEvent[] }) {
  const [expanded, setExpanded] = React.useState(false);
  const eventKey = events.map((event) => event.id).join("|");
  React.useEffect(() => {
    setExpanded(false);
  }, [eventKey]);

  const visibleEvents = expanded ? events : events.slice(0, 4);
  const hiddenCount = Math.max(events.length - visibleEvents.length, 0);

  return (
    <div className="event-list">
      {events.length === 0 && <div className="empty-list">No events in this view.</div>}
      {visibleEvents.map((event) => (
        <div key={event.id} className="event-row">
          <span>{Number(event.event_date.slice(8, 10))}</span>
          <strong>{event.bank_symbol ?? "System"}</strong>
          <strong>{event.title}</strong>
        </div>
      ))}
      {events.length > 4 && (
        <button type="button" className="event-expand-button" onClick={() => setExpanded((current) => !current)}>
          {expanded ? "Show less" : `Show ${hiddenCount} more`}
        </button>
      )}
    </div>
  );
}

type ReportWindowMode = "download" | "subscribe";

interface ReportWindowState {
  mode: ReportWindowMode;
  reportType: string;
}

interface ReportVersionFilters {
  category: string;
  bank: string;
  year: string;
  quarter: string;
}

interface ReportBankOption {
  symbol: string;
  name: string;
  category: string;
}

function reportTypeLabel(reportType: string): string {
  return reportType
    .split("_")
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

function quarterNumber(quarter: string): number {
  const numeric = Number(quarter.replace(/\D/g, ""));
  return Number.isFinite(numeric) ? numeric : 0;
}

function sortReportsByPeriod(reports: ReportSummary[]): ReportSummary[] {
  return [...reports].sort((a, b) => (
    b.fiscal_year - a.fiscal_year
    || quarterNumber(b.quarter) - quarterNumber(a.quarter)
    || a.bank_category.localeCompare(b.bank_category)
    || a.bank_symbol.localeCompare(b.bank_symbol)
  ));
}

function reportVersionMatchesFilters(report: ReportSummary, filters: ReportVersionFilters): boolean {
  return (!filters.category || report.bank_category === filters.category)
    && (!filters.bank || report.bank_symbol === filters.bank)
    && (!filters.year || String(report.fiscal_year) === filters.year)
    && (!filters.quarter || report.quarter === filters.quarter);
}

function hasReportVersionFilters(filters: ReportVersionFilters): boolean {
  return Boolean(filters.category || filters.bank || filters.year || filters.quarter);
}

function reportBankOptions(reports: ReportSummary[]): ReportBankOption[] {
  const banks = new Map<string, ReportBankOption>();
  reports.forEach((report) => {
    if (!banks.has(report.bank_symbol)) {
      banks.set(report.bank_symbol, {
        symbol: report.bank_symbol,
        name: report.bank_name,
        category: report.bank_category
      });
    }
  });
  return [...banks.values()].sort((a, b) => a.symbol.localeCompare(b.symbol));
}

function triggerReportDownloads(reports: ReportSummary[]) {
  reports.forEach((report) => {
    const link = document.createElement("a");
    link.href = report.download_url;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
  });
}

function ReportDownloaderPanel({
  reports,
  subscribeReportSelection
}: {
  reports: ReportSearchResponse | null;
  subscribeReportSelection: (
    reportType: string,
    selectedBankSymbols: string[],
    selectedCategory: string,
    visibleBankSymbols: string[]
  ) => Promise<void>;
}) {
  const [activeWindow, setActiveWindow] = React.useState<ReportWindowState | null>(null);
  const reportRows = React.useMemo(() => {
    const allReports = reports?.reports ?? [];
    const reportTypes = reports?.report_types?.length
      ? reports.report_types
      : uniqueSorted(allReports.map((report) => report.report_type));
    return reportTypes.map((reportType) => {
      const versions = sortReportsByPeriod(allReports.filter((report) => report.report_type === reportType));
      return {
        reportType,
        title: reportTypeLabel(reportType),
        description: versions[0]?.description ?? "Generated Aegis report.",
        versionCount: versions.length
      };
    });
  }, [reports]);

  return (
    <div className="report-pane">
      <div className="report-list report-type-list">
        {reportRows.length === 0 && <div className="empty-list">No generated reports available.</div>}
        {reportRows.map((report) => (
          <div className="report-row report-type-row" key={report.reportType}>
            <div>
              <strong>{report.title}</strong>
              <small>{report.versionCount} available versions</small>
            </div>
            <button
              type="button"
              className="icon-command"
              title={`Download ${report.title}`}
              aria-label={`Download ${report.title}`}
              onClick={() => setActiveWindow({ mode: "download", reportType: report.reportType })}
            >
              <Download size={15} />
            </button>
            <button
              type="button"
              className="icon-command"
              title={`Subscribe to ${report.title}`}
              aria-label={`Subscribe to ${report.title}`}
              onClick={() => setActiveWindow({ mode: "subscribe", reportType: report.reportType })}
            >
              <Bell size={15} />
            </button>
          </div>
        ))}
      </div>
      {activeWindow && (
        <ReportSelectionWindow
          mode={activeWindow.mode}
          reportType={activeWindow.reportType}
          reports={(reports?.reports ?? []).filter((report) => report.report_type === activeWindow.reportType)}
          onClose={() => setActiveWindow(null)}
          subscribeReportSelection={subscribeReportSelection}
        />
      )}
    </div>
  );
}

function ReportSelectionWindow({
  mode,
  reportType,
  reports,
  onClose,
  subscribeReportSelection
}: {
  mode: ReportWindowMode;
  reportType: string;
  reports: ReportSummary[];
  onClose: () => void;
  subscribeReportSelection: (
    reportType: string,
    selectedBankSymbols: string[],
    selectedCategory: string,
    visibleBankSymbols: string[]
  ) => Promise<void>;
}) {
  const [downloadFilters, setDownloadFilters] = React.useState<ReportVersionFilters>({
    category: "",
    bank: "",
    year: "",
    quarter: ""
  });
  const [selectedReportIds, setSelectedReportIds] = React.useState<Set<number>>(new Set());
  const [subscribeCategory, setSubscribeCategory] = React.useState("");
  const [selectedBankSymbols, setSelectedBankSymbols] = React.useState<Set<string>>(new Set());
  const [isSavingSubscription, setIsSavingSubscription] = React.useState(false);
  const sortedReports = React.useMemo(() => sortReportsByPeriod(reports), [reports]);
  const filteredReports = React.useMemo(
    () => sortedReports.filter((report) => reportVersionMatchesFilters(report, downloadFilters)),
    [downloadFilters, sortedReports]
  );
  const allBanks = React.useMemo(() => reportBankOptions(sortedReports), [sortedReports]);
  const visibleBanks = React.useMemo(
    () => subscribeCategory ? allBanks.filter((bank) => bank.category === subscribeCategory) : allBanks,
    [allBanks, subscribeCategory]
  );
  const selectedReports = sortedReports.filter((report) => selectedReportIds.has(report.id));
  const visibleBankSymbols = visibleBanks.map((bank) => bank.symbol);
  const categoryOptions = uniqueSorted(sortedReports.map((report) => report.bank_category));
  const bankFilterRows = downloadFilters.category
    ? sortedReports.filter((report) => report.bank_category === downloadFilters.category)
    : sortedReports;
  const bankOptions = reportBankOptions(bankFilterRows);
  const periodFilterRows = downloadFilters.bank
    ? bankFilterRows.filter((report) => report.bank_symbol === downloadFilters.bank)
    : bankFilterRows;
  const yearOptions = uniqueSorted(periodFilterRows.map((report) => report.fiscal_year)).sort((a, b) => Number(b) - Number(a));
  const quarterFilterRows = downloadFilters.year
    ? periodFilterRows.filter((report) => String(report.fiscal_year) === downloadFilters.year)
    : periodFilterRows;
  const quarterOptions = uniqueSorted(quarterFilterRows.map((report) => report.quarter));

  React.useEffect(() => {
    setDownloadFilters({ category: "", bank: "", year: "", quarter: "" });
    setSelectedReportIds(new Set());
    setSubscribeCategory("");
    setSelectedBankSymbols(new Set());
    setIsSavingSubscription(false);
  }, [mode, reportType]);

  function updateDownloadFilters(patch: Partial<ReportVersionFilters>) {
    const nextFilters = { ...downloadFilters, ...patch };
    if (patch.category !== undefined) nextFilters.bank = "";
    const nextReports = sortedReports.filter((report) => reportVersionMatchesFilters(report, nextFilters));
    setDownloadFilters(nextFilters);
    setSelectedReportIds(new Set(hasReportVersionFilters(nextFilters) ? nextReports.map((report) => report.id) : []));
  }

  function toggleReportSelection(reportId: number) {
    setSelectedReportIds((current) => {
      const next = new Set(current);
      if (next.has(reportId)) next.delete(reportId);
      else next.add(reportId);
      return next;
    });
  }

  function updateSubscribeCategory(category: string) {
    const nextBanks = category ? allBanks.filter((bank) => bank.category === category).map((bank) => bank.symbol) : [];
    setSubscribeCategory(category);
    setSelectedBankSymbols(new Set(nextBanks));
  }

  function toggleBankSelection(bankSymbol: string) {
    setSelectedBankSymbols((current) => {
      const next = new Set(current);
      if (next.has(bankSymbol)) next.delete(bankSymbol);
      else next.add(bankSymbol);
      return next;
    });
  }

  async function saveSubscription() {
    setIsSavingSubscription(true);
    try {
      await subscribeReportSelection(reportType, [...selectedBankSymbols], subscribeCategory, visibleBankSymbols);
      onClose();
    } finally {
      setIsSavingSubscription(false);
    }
  }

  return (
    <div className="report-window-backdrop" role="presentation">
      <section className="report-window" role="dialog" aria-modal="true" aria-label={`${mode === "download" ? "Download" : "Subscribe"} ${reportTypeLabel(reportType)}`}>
        <header className="report-window-header">
          <div>
            <strong>{mode === "download" ? "Download Report" : "Subscribe to Report"}</strong>
            <small>{reportTypeLabel(reportType)}</small>
          </div>
          <button type="button" className="icon-command" aria-label="Close report window" onClick={onClose}>
            <X size={15} />
          </button>
        </header>

        {mode === "download" ? (
          <>
            <div className="report-window-list report-version-list">
              {filteredReports.length === 0 && <div className="empty-list">No report versions match these filters.</div>}
              {filteredReports.map((report) => (
                <label className="report-check-row" key={report.id}>
                  <input
                    type="checkbox"
                    checked={selectedReportIds.has(report.id)}
                    onChange={() => toggleReportSelection(report.id)}
                  />
                  <span>
                    <strong>{report.bank_symbol}</strong>
                    <small>{report.bank_category}</small>
                  </span>
                  <span>{report.quarter} {report.fiscal_year}</span>
                  <span>{new Date(report.generated_at).toLocaleDateString()}</span>
                </label>
              ))}
            </div>
            <div className="report-window-filters">
              <select value={downloadFilters.category} onChange={(event) => updateDownloadFilters({ category: event.target.value })}>
                <option value="">Category</option>
                {categoryOptions.map((category) => <option key={category} value={category}>{category}</option>)}
              </select>
              <select value={downloadFilters.bank} onChange={(event) => updateDownloadFilters({ bank: event.target.value })}>
                <option value="">Bank</option>
                {bankOptions.map((bank) => <option key={bank.symbol} value={bank.symbol}>{bank.symbol} - {bank.name}</option>)}
              </select>
              <select value={downloadFilters.year} onChange={(event) => updateDownloadFilters({ year: event.target.value })}>
                <option value="">Year</option>
                {yearOptions.map((year) => <option key={year} value={year}>{year}</option>)}
              </select>
              <select value={downloadFilters.quarter} onChange={(event) => updateDownloadFilters({ quarter: event.target.value })}>
                <option value="">Quarter</option>
                {quarterOptions.map((quarter) => <option key={quarter} value={quarter}>{quarter}</option>)}
              </select>
            </div>
            <footer className="report-window-actions">
              <button type="button" className="text-command" onClick={onClose}>Close</button>
              <button
                type="button"
                className="text-command primary-action"
                disabled={selectedReports.length === 0}
                onClick={() => triggerReportDownloads(selectedReports)}
              >
                Download selected
              </button>
            </footer>
          </>
        ) : (
          <>
            <div className="report-window-list report-bank-list">
              {visibleBanks.length === 0 && <div className="empty-list">No banks match this category.</div>}
              {visibleBanks.map((bank) => (
                <label className="report-check-row bank-check-row" key={bank.symbol}>
                  <input
                    type="checkbox"
                    checked={selectedBankSymbols.has(bank.symbol)}
                    onChange={() => toggleBankSelection(bank.symbol)}
                  />
                  <span>
                    <strong>{bank.symbol}</strong>
                    <small>{bank.name}</small>
                  </span>
                  <span>{bank.category}</span>
                </label>
              ))}
            </div>
            <div className="report-window-filters subscribe-window-filters">
              <select value={subscribeCategory} onChange={(event) => updateSubscribeCategory(event.target.value)}>
                <option value="">Category</option>
                {categoryOptions.map((category) => <option key={category} value={category}>{category}</option>)}
              </select>
            </div>
            <footer className="report-window-actions">
              <button type="button" className="text-command" onClick={onClose}>Close</button>
              <button
                type="button"
                className="text-command primary-action"
                disabled={selectedBankSymbols.size === 0 || isSavingSubscription}
                onClick={() => saveSubscription().catch(console.error)}
              >
                Subscribe selected
              </button>
            </footer>
          </>
        )}
      </section>
    </div>
  );
}

function ReportSchedulerPanel({
  subscriptions,
  schedulerScope,
  setSchedulerScope,
  categoryOptions,
  bankOptions,
  subscribe
}: {
  subscriptions: ReportSubscriptionResponse | null;
  schedulerScope: SchedulerScope;
  setSchedulerScope: React.Dispatch<React.SetStateAction<SchedulerScope>>;
  categoryOptions: string[];
  bankOptions: Array<[string, string]>;
  subscribe: (reportType: string) => Promise<void>;
}) {
  const reportTypes = subscriptions?.report_types ?? [];
  return (
    <div className="scheduler-pane">
      <div className="scheduler-controls">
        <select value={schedulerScope.scopeType} onChange={(event) => setSchedulerScope((current) => ({ ...current, scopeType: event.target.value }))}>
          <option value="all_banks">All banks</option>
          <option value="category">Category</option>
          <option value="bank">Bank</option>
        </select>
        {schedulerScope.scopeType === "category" && (
          <select value={schedulerScope.category} onChange={(event) => setSchedulerScope((current) => ({ ...current, category: event.target.value }))}>
            <option value="">Select category</option>
            {categoryOptions.map((category) => <option key={category} value={category}>{category}</option>)}
          </select>
        )}
        {schedulerScope.scopeType === "bank" && (
          <select value={schedulerScope.bank} onChange={(event) => setSchedulerScope((current) => ({ ...current, bank: event.target.value }))}>
            <option value="">Select bank</option>
            {bankOptions.map(([symbol, name]) => <option key={symbol} value={symbol}>{symbol} - {name}</option>)}
          </select>
        )}
      </div>
      <div className="subscription-list">
        {reportTypes.map((reportType) => (
          <div className="subscription-row" key={reportType}>
            <div>
              <strong>{reportType.replace("_", " ")}</strong>
              <small>Email when new reports become available</small>
            </div>
            <button type="button" className="text-command" onClick={() => subscribe(reportType)}>
              Subscribe
            </button>
          </div>
        ))}
      </div>
      <div className="current-subscriptions">
        {(subscriptions?.subscriptions ?? []).map((subscription) => (
          <small key={subscription.id}>
            {subscription.report_type} / {subscription.scope_type.replace("_", " ")}
            {subscription.bank_category ? ` / ${subscription.bank_category}` : ""}
            {subscription.bank_symbol ? ` / ${subscription.bank_symbol}` : ""}
          </small>
        ))}
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const isFinalResponse = Boolean(message.finalShell);
  return (
    <div className={`message ${message.role} ${isFinalResponse ? "final-response-message" : ""}`}>
      <span>{message.role === "assistant" ? "agent" : message.role}</span>
      {message.finalShell?.summary && (
        <section className="final-response-shell">
          <small>{stripEvidenceMarkers(message.finalShell.summary.eyebrow ?? "Aegis")}</small>
          <strong>{stripEvidenceMarkers(message.finalShell.summary.headline)}</strong>
          {message.finalShell.summary.dek && <p>{stripEvidenceMarkers(message.finalShell.summary.dek)}</p>}
          {(message.finalShell.tiles ?? []).length > 0 && (
            <div className="final-response-tiles">
              {(message.finalShell.tiles ?? []).slice(0, 4).map((tile, index) => (
                <div className="final-response-tile" key={`${tile.label}-${index}`}>
                  <span>{stripEvidenceMarkers(tile.label)}</span>
                  <strong>{stripEvidenceMarkers(tile.value)}</strong>
                  {tile.context && <small>{stripEvidenceMarkers(tile.context)}</small>}
                </div>
              ))}
            </div>
          )}
        </section>
      )}
      <MarkdownContent
        content={message.content}
        className={isFinalResponse ? "final-response-body" : "message-body"}
        drafting={isFinalResponse && !message.content.trim()}
      />
    </div>
  );
}

function WidgetView({
  widget,
  runWidgetAction
}: {
  widget: HtmlWidget;
  runWidgetAction: (action: HtmlWidget["actions"][number], widget: HtmlWidget) => void;
}) {
  if (widget.kind === "data_availability") {
    return <DataAvailabilityChatWidget widget={widget} runWidgetAction={runWidgetAction} />;
  }

  return (
    <article className={`widget ${widget.status}`}>
      <div className="widget-header">
        <Sparkles size={16} />
        <strong>{widget.title}</strong>
        <span>{widget.status}</span>
      </div>
      <div className="widget-html" dangerouslySetInnerHTML={{ __html: widget.html }} />
      {widget.actions.length > 0 && (
        <div className="widget-actions">
          {widget.actions.slice(0, 6).map((action) => (
            <button key={action.id} type="button" className="text-command" onClick={() => runWidgetAction(action, widget)}>
              {action.label}
            </button>
          ))}
        </div>
      )}
    </article>
  );
}

function actionForAvailabilityRow(
  row: DataAvailabilityRow,
  actions: HtmlWidget["actions"]
): HtmlWidget["actions"][number] | undefined {
  return actions.find((action) => {
    const payload = action.payload;
    const banks = Array.isArray(payload.bank_symbols) ? payload.bank_symbols.map(String) : [];
    const years = Array.isArray(payload.fiscal_years) ? payload.fiscal_years.map(Number) : [];
    const quarters = Array.isArray(payload.quarters) ? payload.quarters.map(String) : [];
    return banks.includes(row.bank_symbol) && years.includes(row.fiscal_year) && quarters.includes(row.quarter);
  });
}

function DataAvailabilityChatWidget({
  widget,
  runWidgetAction
}: {
  widget: HtmlWidget;
  runWidgetAction: (action: HtmlWidget["actions"][number], widget: HtmlWidget) => void;
}) {
  const response = asAvailabilityResponse(widget.data);

  if (widget.status === "failed") {
    return (
      <article className="availability-chat-widget failed">
        <div className="availability-chat-header">
          <Database size={15} />
          <strong>{widget.title}</strong>
        </div>
        <div className="availability-chat-error" dangerouslySetInnerHTML={{ __html: widget.html }} />
      </article>
    );
  }

  if (!response || widget.status === "running" || widget.status === "pending") {
    return (
      <article className="availability-chat-widget running" aria-live="polite" aria-label="Checking data availability">
        <div className="availability-chat-header">
          <Database size={15} />
          <strong>{widget.title}</strong>
        </div>
        <div className="availability-chat-loading">
          <p>AEGIS is checking the data coverage.</p>
        </div>
      </article>
    );
  }

  const rows = availabilityDisplayRows(response.rows, 24);

  return (
    <article className="availability-chat-widget complete">
      <div className="availability-chat-header">
        <Database size={15} />
        <strong>{widget.title}</strong>
      </div>
      {rows.length === 0 ? (
        <div className="availability-chat-empty">No coverage found for the selected context.</div>
      ) : (
        <div className="availability-chat-table-wrap" role="region" aria-label="Data availability results" tabIndex={0}>
          <table className="availability-chat-table">
            <thead>
              <tr>
                <th>Period</th>
                <th>Category</th>
                <th>Bank</th>
                {SOURCE_OPTIONS.map(([id, label]) => (
                  <th key={id} className="source-heading">
                    <span>{label}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((display) => {
                const rowAction = actionForAvailabilityRow(display.row, widget.actions);
                return (
                  <tr
                    key={`${display.row.bank_symbol}-${display.row.fiscal_year}-${display.row.quarter}`}
                    className={[
                      rowAction ? "clickable" : "",
                      display.startsPeriod ? "period-break" : "",
                      display.startsCategory ? "category-break" : ""
                    ].filter(Boolean).join(" ")}
                    onClick={() => rowAction && runWidgetAction(rowAction, widget)}
                  >
                    {display.periodRowSpan > 0 && (
                      <td rowSpan={display.periodRowSpan} className="merged-cell period-cell">{display.period}</td>
                    )}
                    {display.categoryRowSpan > 0 && (
                      <td rowSpan={display.categoryRowSpan} className="merged-cell category-cell">{display.category}</td>
                    )}
                    {display.bankRowSpan > 0 && (
                      <td rowSpan={display.bankRowSpan} className="merged-cell bank-cell"><strong>{display.bank}</strong></td>
                    )}
                    {SOURCE_OPTIONS.map(([id]) => (
                      <td key={id} className={display.row.source_ids.includes(id) ? "source-cell has-source" : "source-cell"}>
                        {display.row.source_ids.includes(id) ? <SourceIcon sourceId={id} size={13} /> : null}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {response.rows.length > rows.length && (
        <div className="availability-chat-note">Showing first {rows.length} rows in chat.</div>
      )}
    </article>
  );
}

function ThinkingBox() {
  return (
    <article className="thinking-box" aria-live="polite" aria-label="Aegis thinking">
      <div className="thinking-box-header">
        <Sparkles size={15} />
        <strong>Thinking</strong>
      </div>
      <p>AEGIS is deciding what to do next.</p>
    </article>
  );
}

function PreviewPanel({ preview }: { preview: PreviewTarget }) {
  return (
    <div className="preview-pane">
      {preview.url && (
        <div className="app-action-row">
          <a href={preview.url} target="_blank" rel="noreferrer" title="Open externally" className="icon-command">
            <ExternalLink size={16} />
          </a>
        </div>
      )}
      {preview.url ? (
        <iframe title={preview.title} src={preview.url} sandbox="allow-same-origin allow-downloads" />
      ) : preview.artifactHtml ? (
        <iframe title={preview.title} srcDoc={preview.artifactHtml} sandbox="allow-same-origin" />
      ) : (
        <div className="empty-preview">Click a link in the chat or select an artifact below</div>
      )}
    </div>
  );
}

function artifactKindLabel(artifact: Artifact): string {
  const kind = artifact.kind.toLowerCase();
  if (kind.includes("deep") || kind.includes("long")) return "Deep search";
  if (kind.includes("report")) return "Report";
  if (kind.includes("quick") || kind.includes("short") || kind.includes("search") || kind.includes("research")) {
    return "Quick search";
  }
  return "Artifact";
}

function artifactKindClass(artifact: Artifact): string {
  return artifactKindLabel(artifact).toLowerCase().replace(/\s+/g, "-");
}

function artifactCreatedTime(artifact: Artifact): number {
  const timestamp = new Date(artifact.created_at).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function artifactCreatedParts(artifact: Artifact): { dateLabel: string; timeLabel: string; dateTime: string } {
  const timestamp = artifactCreatedTime(artifact);
  if (!timestamp) {
    return {
      dateLabel: "Unknown date",
      timeLabel: "Unknown time",
      dateTime: artifact.created_at
    };
  }
  const createdAt = new Date(timestamp);
  return {
    dateLabel: createdAt.toLocaleDateString([], {
      month: "numeric",
      day: "numeric",
      year: "2-digit"
    }),
    timeLabel: createdAt.toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit"
    }),
    dateTime: createdAt.toISOString()
  };
}

function artifactKindIcon(kindLabel: string): React.ReactNode {
  if (kindLabel === "Deep search") return <Files size={20} />;
  if (kindLabel === "Report") return <FileChartColumnIncreasing size={20} />;
  return <FileText size={20} />;
}

function ArtifactPanel({
  artifacts,
  openArtifact
}: {
  artifacts: Artifact[];
  openArtifact: (artifact: Artifact) => void;
}) {
  const viewportRef = React.useRef<HTMLDivElement | null>(null);
  const [scrollState, setScrollState] = React.useState({ canScrollUp: false, canScrollDown: false });
  const sortedArtifacts = React.useMemo(
    () => [...artifacts].sort((a, b) => artifactCreatedTime(b) - artifactCreatedTime(a)),
    [artifacts]
  );

  const updateArtifactScrollState = React.useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const maxScrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
    const next = {
      canScrollUp: viewport.scrollTop > 1,
      canScrollDown: viewport.scrollTop < maxScrollTop - 8
    };
    setScrollState((current) => (
      current.canScrollUp === next.canScrollUp && current.canScrollDown === next.canScrollDown
        ? current
        : next
    ));
  }, []);

  React.useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    viewport.scrollTop = 0;
    updateArtifactScrollState();
    const animationFrame = window.requestAnimationFrame(updateArtifactScrollState);
    const resizeObserver = new ResizeObserver(updateArtifactScrollState);
    resizeObserver.observe(viewport);
    viewport.addEventListener("scroll", updateArtifactScrollState);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      resizeObserver.disconnect();
      viewport.removeEventListener("scroll", updateArtifactScrollState);
    };
  }, [sortedArtifacts.length, updateArtifactScrollState]);

  function scrollArtifactRows(direction: "up" | "down") {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const firstTile = viewport.querySelector<HTMLElement>(".artifact-tile, .artifact-empty-tile");
    const styles = window.getComputedStyle(viewport);
    const rowGap = Number.parseFloat(styles.rowGap) || 0;
    const rowStep = firstTile ? firstTile.getBoundingClientRect().height + rowGap : viewport.clientHeight;
    viewport.scrollBy({ top: direction === "down" ? rowStep : -rowStep, behavior: "smooth" });
    window.setTimeout(updateArtifactScrollState, 240);
  }

  return (
    <div className="artifact-pane">
      <div className="artifact-scroll-viewport" ref={viewportRef} aria-label="Session artifacts">
        <div className="artifact-grid">
        {sortedArtifacts.length === 0 ? (
          <div className="artifact-empty-tile">
            <strong>No artifacts yet</strong>
            <span>Generated research and report artifacts will appear here.</span>
          </div>
        ) : sortedArtifacts.map((artifact) => {
          const kindLabel = artifactKindLabel(artifact);
          const createdAt = artifactCreatedParts(artifact);
          return (
            <button
              key={artifact.id}
              type="button"
              className={`artifact-tile ${artifactKindClass(artifact)}`}
              onClick={() => openArtifact(artifact)}
            >
              <strong>{artifact.title}</strong>
              <span className="artifact-tile-footer">
                <span className="artifact-meta">
                  <span>{kindLabel}</span>
                  <time dateTime={createdAt.dateTime}>{createdAt.dateLabel}</time>
                  <time dateTime={createdAt.dateTime}>{createdAt.timeLabel}</time>
                </span>
                <span className="artifact-kind-icon" aria-hidden="true">
                  {artifactKindIcon(kindLabel)}
                </span>
              </span>
            </button>
          );
        })}
        </div>
      </div>
      <div className="artifact-scroll-controls" aria-label="Artifact row controls">
        <button
          type="button"
          className="icon-command"
          title="Previous artifact row"
          aria-label="Previous artifact row"
          disabled={!scrollState.canScrollUp}
          onClick={() => scrollArtifactRows("up")}
        >
          <ChevronUp size={16} />
        </button>
        <button
          type="button"
          className="icon-command"
          title="Next artifact row"
          aria-label="Next artifact row"
          disabled={!scrollState.canScrollDown}
          onClick={() => scrollArtifactRows("down")}
        >
          <ChevronDown size={16} />
        </button>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
