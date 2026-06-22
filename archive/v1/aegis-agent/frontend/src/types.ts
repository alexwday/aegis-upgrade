export type WidgetStatus = "pending" | "running" | "complete" | "failed";

export interface WidgetAction {
  id: string;
  label: string;
  action_type: string;
  payload: Record<string, unknown>;
}

export interface HtmlWidget {
  id: string;
  kind: string;
  title: string;
  status: WidgetStatus;
  html: string;
  data: Record<string, unknown>;
  actions: WidgetAction[];
  created_at: string;
  updated_at: string;
}

export interface DataAvailabilityRow {
  bank_id: number;
  bank_name: string;
  bank_symbol: string;
  bank_category: string;
  bank_category_id: string;
  bank_tags: string[];
  fiscal_year: number;
  quarter: string;
  source_ids: string[];
  last_refreshed_at: string | null;
}

export interface SourceSummary {
  id: string;
  label: string;
  available_rows: number;
}

export interface DataAvailabilityResponse {
  rows: DataAvailabilityRow[];
  missing: Array<{
    bank_symbol: string;
    bank_name: string;
    fiscal_year: number;
    quarter: string;
    missing_source_ids: string[];
  }>;
  sources: SourceSummary[];
  bank_categories: string[];
  fiscal_years: number[];
  quarters: string[];
  generated_at: string;
}

export interface DocumentSummary {
  source_id: string;
  source_label: string;
  file_id: string;
  bank_symbol: string;
  bank_category: string;
  fiscal_year: string;
  quarter: string;
  filename: string;
  file_type: string;
  preview_url: string;
  download_url: string;
  preview_status: "ready" | "missing" | "error";
  preview_error: string | null;
  updated_at: string | null;
}

export interface DocumentSearchResponse {
  documents: DocumentSummary[];
  total: number;
  generated_at: string;
}

export interface ReleaseEvent {
  id: string;
  event_type: string;
  title: string;
  event_date: string;
  bank_symbol: string | null;
  bank_name: string | null;
  bank_category: string;
  fiscal_year: number | null;
  quarter: string | null;
  source_id: string | null;
}

export interface ReleaseCalendarResponse {
  month: string;
  events: ReleaseEvent[];
  event_types: string[];
  generated_at: string;
}

export interface ReportSummary {
  id: number;
  title: string;
  description: string;
  report_type: string;
  bank_id: number;
  bank_name: string;
  bank_symbol: string;
  bank_category: string;
  fiscal_year: number;
  quarter: string;
  generated_at: string;
  preview_url: string;
  download_url: string;
}

export interface ReportSearchResponse {
  reports: ReportSummary[];
  report_types: string[];
  generated_at: string;
}

export interface ReportSubscription {
  id: string;
  report_type: string;
  scope_type: "all_banks" | "category" | "bank";
  bank_category: string | null;
  bank_symbol: string | null;
  delivery: "email";
  created_at: string;
}

export interface ReportSubscriptionResponse {
  report_types: string[];
  subscriptions: ReportSubscription[];
}

export interface Artifact {
  id: string;
  session_id: string;
  kind: string;
  title: string;
  html: string;
  source_widget_ids: string[];
  evidence_ids: string[];
  created_at: string;
}

export interface ChatMessage {
  id: string;
  role: "system" | "user" | "assistant";
  content: string;
}

export interface PreviewTarget {
  kind: "source_document" | "artifact" | "html" | "report" | "empty";
  title: string;
  url?: string;
  artifactHtml?: string;
  source_id?: string;
  file_id?: string;
  artifact_id?: string;
  location?: string;
}

export interface Filters {
  source_ids: string[];
  bank_symbols: string[];
  bank_categories: string[];
  fiscal_years: number[];
  quarters: string[];
  keyword: string;
}

export interface V2Event {
  type: string;
  session_id: string;
  payload: Record<string, unknown>;
  event_id: string;
  timestamp: string;
}
