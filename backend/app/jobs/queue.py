"""Job queue backed by the SQLite `jobs` table."""

from __future__ import annotations

from app import db


def enqueue(job_id: int) -> None:
    db.ex(
        "UPDATE jobs SET status = 'queued', progress = 0, stage = 'queued', error = NULL WHERE id = ? AND status NOT IN ('queued', 'running')",
        (job_id,),
    )


def claim_next() -> int | None:
    """Atomically move the oldest queued job to 'running'."""
    with db.transaction() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE jobs SET status = 'running', stage = 'starting' WHERE id = ?", (row["id"],))
        return int(row["id"])


def requeue_stale() -> int:
    """On startup, jobs left 'running' by a crash go back to the queue."""
    return db.ex("UPDATE jobs SET status = 'queued', stage = 'requeued after restart' WHERE status = 'running'")
