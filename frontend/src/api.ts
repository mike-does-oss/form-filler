// Typed client for the FastAPI backend. All requests are same-origin (/api/...);
// nothing ever leaves the machine.

export type FieldType =
  | "text" | "multiline" | "number" | "currency" | "date" | "checkbox" | "choice" | "email" | "phone" | "signature";
export const FIELD_TYPES: FieldType[] = [
  "text", "multiline", "number", "currency", "date", "checkbox", "choice", "email", "phone", "signature",
];
export type FieldKind = "lookup" | "judgment";
export type FormType = "acroform" | "docx" | "flat_pdf";
export type Bbox = [number, number, number, number];
export type Status = "verified" | "unverified" | "derived" | "conflict" | "not_found" | "judged" | "cannot_determine";
export type Confidence = "high" | "medium" | "low";
export type ReviewStatus = "pending" | "accepted" | "edited" | "not_applicable";

export interface FieldDef {
  id?: number | null;
  key: string;
  label: string;
  type: FieldType | string;
  kind: FieldKind;
  section?: string | null;
  required: boolean;
  options: string[];
  format_hint?: string | null;
  instructions?: string | null;
  page?: number | null;
  bbox?: Bbox | null;
  source_ref?: string | null;
  meta?: Record<string, unknown>;
  order_index: number;
}

export interface Form {
  id: number;
  filename: string;
  form_type: FormType;
  fingerprint?: string;
  template_id: number | null;
  template_name: string | null;
  template_matched?: boolean;
  page_count: number;
  created_at?: string;
  fields: FieldDef[];
}

export interface PageInfo { page: number; width: number; height: number; ocr_confidence?: number | null }
export interface Doc {
  id: number;
  filename: string;
  mime?: string;
  page_count: number | null;
  ocr_used: boolean;
  status?: string;
  pages: PageInfo[];
}

export type JobStatus = "created" | "queued" | "running" | "done" | "failed";
export interface Job {
  id: number;
  form_id: number;
  form?: { id: number; filename: string; form_type: FormType; template_id: number | null } | null;
  status: JobStatus;
  progress: number;
  stage: string | null;
  error: string | null;
  stats: Record<string, unknown>;
  created_at?: string;
  reviewed: number;
  total_fields: number;
  documents: Doc[];
}
export interface JobSummary {
  id: number; form_id: number; status: JobStatus; progress: number; stage: string | null;
  created_at: string; batch_id: string | null; form_filename: string;
}

export interface Evidence {
  document_id: number | null;
  chunk_id?: number | null;
  page: number | null;
  bbox: Bbox | null;
  boxes: Bbox[];
  quote: string;
  verified: boolean;
  score?: number;
  value_in_quote?: boolean;
  source?: string;
}
export interface Candidate { value: unknown; evidence: Evidence[] }
export interface DerivationInput {
  value: unknown; quote?: string; document_id?: number | null; page?: number | null;
  bbox?: Bbox | null; boxes?: Bbox[]; verified?: boolean; chunk_id?: number | null;
}
export interface FactUsed {
  fact_key: string; label: string; value: string | null; status: string; source: string;
  evidence: Evidence[]; used?: boolean;
}
export interface MissingFact { fact_key: string; label: string; question: string | null }
export interface Fact {
  fact_key: string; label: string; type: string; question: string | null; value: string | null;
  status: string; source: string; evidence: Evidence[]; confidence?: string;
}
export interface AppliedRule {
  rule_type?: "code" | "rubric"; rule_code?: string | null; rubric_text?: string | null;
  rule_params?: Record<string, unknown>; votes?: Record<string, number>; runs?: number; status?: string;
}

export interface FieldValue {
  field_id: number;
  key: string;
  label: string;
  type: FieldType | string;
  kind: FieldKind;
  section: string | null;
  required: boolean;
  options: string[];
  format_hint: string | null;
  instructions: string | null;
  page: number | null;
  bbox: Bbox | null;
  order_index: number;
  value: unknown;
  final_value: unknown;
  status: Status | null;
  confidence: Confidence | null;
  evidence: Evidence[];
  candidates: Candidate[];
  validator_errors: string[];
  derivation: { op: string; inputs: DerivationInput[]; result: unknown } | null;
  reasoning: string | null;
  facts_used: FactUsed[];
  missing_facts: MissingFact[];
  run_agreement: number | null;
  rule: AppliedRule | null;
  review_status: ReviewStatus;
  user_edited: boolean;
  facts: Fact[];
  requires_manual_signing: boolean;
}

export interface Health {
  status: "ok" | "degraded";
  llm: {
    backend?: string; base_url?: string; reachable: boolean;
    models: Record<string, { name: string; available: boolean }>;
    available_models?: string[]; error?: string;
  };
  ocr: { configured_engine?: string; active_engine: string; tesseract?: boolean; [k: string]: unknown };
  storage?: { data_dir: string };
  config: Record<string, unknown>;
}

export interface FactDefT { key: string; label: string; type: string; question: string | null; required: boolean }
export interface Rule {
  id: number; template_id: number; field_key: string; depends_on: string[]; facts: FactDefT[];
  rule_type: "code" | "rubric"; rule_code: string | null; rule_params: Record<string, unknown>;
  rubric_text: string | null; status: "draft" | "saved"; flagged: boolean; flag_reason: string | null; updated_at: string;
}
export interface Correction {
  id: number; field_key: string; field_label: string | null; model_value: string | null; user_value: string | null;
  user_note: string | null; facts_json: string | null; created_at: string;
}
export interface TemplateSummary {
  id: number; name: string; form_type: FormType; created_at: string; updated_at: string;
  form_count: number; correction_count: number; rule_count: number; flagged_count: number;
}
export interface Template {
  id: number; name: string; form_type: FormType; fingerprint: string; fields: FieldDef[];
  cross_field_rules: unknown[]; rules: Rule[]; latest_form_id: number | null; corrections: Correction[];
}
export interface RegistryEntry { name: string; description: string; params_example: Record<string, unknown> }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function req<T>(method: string, url: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const init: RequestInit = { method, signal };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers = { "Content-Type": "application/json" };
  }
  const r = await fetch(url, init);
  if (!r.ok) throw new ApiError(r.status, await errorText(r));
  return (await r.json()) as T;
}

async function errorText(r: Response): Promise<string> {
  const t = await r.text();
  try {
    const j = JSON.parse(t);
    if (typeof j.detail === "string") return j.detail;
    if (Array.isArray(j.detail)) return j.detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join("; ");
    return t;
  } catch {
    return t || `${r.status} ${r.statusText}`;
  }
}

export type ReviewAction = "accept" | "edit" | "choose_candidate" | "not_applicable" | "reset";
export type ExportFormat = "filled" | "flattened" | "json" | "csv";

export const api = {
  health: () => req<Health>("GET", "/api/health"),

  uploadForm: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<Form>("POST", "/api/forms", fd);
  },
  listForms: () => req<Omit<Form, "fields">[]>("GET", "/api/forms"),
  getForm: (id: number) => req<Form>("GET", `/api/forms/${id}`),
  putFields: (id: number, fields: Partial<FieldDef>[], replace = false) =>
    req<Form>("PUT", `/api/forms/${id}/fields`, { fields, replace }),
  detect: (id: number) => req<Form>("POST", `/api/forms/${id}/detect`),
  formPageUrl: (id: number, page: number, dpi = 110) => `/api/forms/${id}/pages/${page}?dpi=${dpi}`,

  createJob: (form_id: number) => req<Job>("POST", "/api/jobs", { form_id }),
  listJobs: () => req<JobSummary[]>("GET", "/api/jobs"),
  getJob: (id: number) => req<Job>("GET", `/api/jobs/${id}`),
  deleteJob: (id: number) => req<{ deleted: number }>("DELETE", `/api/jobs/${id}`),
  uploadDocs: (id: number, files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    return req<Doc[]>("POST", `/api/jobs/${id}/documents`, fd);
  },
  deleteDoc: (id: number, docId: number) => req<{ deleted: number }>("DELETE", `/api/jobs/${id}/documents/${docId}`),
  runJob: (id: number) => req<Job>("POST", `/api/jobs/${id}/run`),
  values: (id: number) => req<{ job_id: number; fields: FieldValue[] }>("GET", `/api/jobs/${id}/values`),
  review: (id: number, fieldId: number, body: { action: ReviewAction; value?: unknown; candidate_index?: number; note?: string }) =>
    req<FieldValue>("PATCH", `/api/jobs/${id}/values/${fieldId}`, body),
  answerFacts: (id: number, fieldId: number, answers: Record<string, unknown>) =>
    req<FieldValue>("POST", `/api/jobs/${id}/values/${fieldId}/facts`, { answers }),
  previewPageUrl: (id: number, page: number, bust: number, dpi = 110) =>
    `/api/jobs/${id}/preview/pages/${page}?dpi=${dpi}&v=${bust}`,
  previewHtmlUrl: (id: number, bust: number) => `/api/jobs/${id}/preview/html?v=${bust}`,

  getDocument: (id: number) => req<Doc>("GET", `/api/documents/${id}`),
  docPageUrl: (id: number, page: number, dpi = 110) => `/api/documents/${id}/pages/${page}?dpi=${dpi}`,

  listTemplates: () => req<TemplateSummary[]>("GET", "/api/templates"),
  getTemplate: (id: number) => req<Template>("GET", `/api/templates/${id}`),
  patchTemplate: (id: number, body: { name?: string }) => req<Template>("PATCH", `/api/templates/${id}`, body),
  putRule: (id: number, key: string, body: Partial<Pick<Rule, "facts" | "rule_type" | "rule_code" | "rule_params" | "rubric_text">> & { status: "draft" | "saved" }) =>
    req<Rule>("PUT", `/api/templates/${id}/rules/${encodeURIComponent(key)}`, body),
  proposeRule: (id: number, key: string) => req<Rule>("POST", `/api/templates/${id}/rules/${encodeURIComponent(key)}/propose`),
  registry: () => req<RegistryEntry[]>("GET", "/api/rules/registry"),
};

/** POST /export and trigger a browser download of the returned file. */
export async function exportJob(id: number, format: ExportFormat): Promise<{ filename: string; warnings: string[] }> {
  const r = await fetch(`/api/jobs/${id}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ format }),
  });
  if (!r.ok) throw new ApiError(r.status, await errorText(r));
  let warnings: string[] = [];
  const wh = r.headers.get("X-Export-Warnings");
  if (wh) {
    try { warnings = JSON.parse(wh); } catch { warnings = [wh]; }
  }
  const cd = r.headers.get("Content-Disposition") ?? "";
  const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(cd);
  const filename = m ? decodeURIComponent(m[1] ?? m[2]) : `job_${id}.${format === "csv" ? "csv" : format === "json" ? "json" : "pdf"}`;
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  return { filename, warnings };
}

export function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}
