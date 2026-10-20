CREATE TABLE IF NOT EXISTS form_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    form_type TEXT NOT NULL,
    field_schema_json TEXT NOT NULL DEFAULT '[]',
    field_mappings_json TEXT NOT NULL DEFAULT '{}',
    cross_field_rules_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    form_type TEXT NOT NULL CHECK (form_type IN ('acroform', 'docx', 'flat_pdf')),
    fingerprint TEXT NOT NULL,
    template_id INTEGER REFERENCES form_templates(id) ON DELETE SET NULL,
    page_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    label TEXT NOT NULL,
    type TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'lookup' CHECK (kind IN ('lookup', 'judgment')),
    section TEXT,
    required INTEGER NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '[]',
    format_hint TEXT,
    instructions TEXT,
    page INTEGER,
    bbox_json TEXT,
    source_ref TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}',
    order_index INTEGER NOT NULL DEFAULT 0,
    UNIQUE (form_id, key)
);

CREATE TABLE IF NOT EXISTS judgment_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES form_templates(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    facts_json TEXT NOT NULL DEFAULT '[]',
    rule_type TEXT NOT NULL DEFAULT 'rubric' CHECK (rule_type IN ('code', 'rubric')),
    rule_code TEXT,
    rule_params_json TEXT NOT NULL DEFAULT '{}',
    rubric_text TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'saved')),
    flagged INTEGER NOT NULL DEFAULT 0,
    flag_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (template_id, field_key)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'created',
    progress REAL NOT NULL DEFAULT 0,
    stage TEXT,
    error TEXT,
    batch_id TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    template_id INTEGER REFERENCES form_templates(id) ON DELETE SET NULL,
    field_key TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    label TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'text',
    question TEXT,
    value TEXT,
    status TEXT,
    confidence TEXT,
    source TEXT NOT NULL DEFAULT 'model',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (job_id, field_key, fact_key)
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    mime TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    ocr_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'uploaded',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    bbox_json TEXT,
    text TEXT NOT NULL,
    embedding BLOB,
    chunk_index INTEGER NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);

-- Page-level word spans used to map quotes back to bboxes.
CREATE TABLE IF NOT EXISTS page_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    width REAL,
    height REAL,
    spans_json TEXT NOT NULL DEFAULT '[]',
    ocr_confidence REAL,
    UNIQUE (document_id, page)
);

CREATE TABLE IF NOT EXISTS field_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    field_id INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
    value TEXT,
    status TEXT NOT NULL CHECK (status IN ('verified', 'unverified', 'derived', 'conflict', 'not_found', 'judged', 'cannot_determine')),
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    validator_errors_json TEXT NOT NULL DEFAULT '[]',
    derivation_json TEXT,
    reasoning TEXT,
    facts_used_json TEXT NOT NULL DEFAULT '[]',
    missing_facts_json TEXT NOT NULL DEFAULT '[]',
    run_agreement REAL,
    rule_json TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending', 'accepted', 'edited', 'not_applicable')),
    user_edited INTEGER NOT NULL DEFAULT 0,
    final_value TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (job_id, field_id)
);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES form_templates(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    field_label TEXT,
    model_value TEXT,
    user_value TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    facts_json TEXT,
    user_note TEXT,
    job_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_corrections_tpl ON corrections(template_id, field_key);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    field_id INTEGER,
    action TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    actor TEXT NOT NULL DEFAULT 'user',
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
