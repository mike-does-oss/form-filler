-- Batch fill: one job per source-document folder.
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    form_id INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
ALTER TABLE jobs ADD COLUMN label TEXT;
CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);
