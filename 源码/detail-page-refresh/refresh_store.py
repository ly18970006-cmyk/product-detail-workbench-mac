"""Persistent, owner-scoped registration and job records for the refresh service."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


def stamp():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def revision(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class StoreConflict(Exception):
    pass


class Store:
    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = root / "refresh.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS registrations (
                    owner TEXT NOT NULL, code TEXT NOT NULL, version TEXT NOT NULL,
                    saved_at TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY (owner, code, version)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, code TEXT NOT NULL,
                    action TEXT NOT NULL, idem TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, saved_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    body TEXT NOT NULL, result TEXT, error TEXT,
                    UNIQUE (owner, action, idem)
                );
                CREATE INDEX IF NOT EXISTS tasks_by_owner ON tasks(owner, code, saved_at);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_registration(self, owner, data):
        version = revision(data)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO registrations VALUES (?, ?, ?, ?, ?)",
                       (owner, data["code"], version, stamp(), canonical(data).decode()))
        return {**data, "version": version}

    def get_registration(self, owner, code, version=None):
        sql = "SELECT data, version FROM registrations WHERE owner = ? AND code = ?"
        params = [owner, code]
        if version is not None:
            sql += " AND version = ?"
            params.append(version)
        with self.connect() as db:
            row = db.execute(sql + " ORDER BY saved_at DESC LIMIT 1", params).fetchone()
        return None if row is None else {**json.loads(row["data"]), "version": row["version"]}

    def enqueue(self, owner, code, action, key, body, *, company=False, company_limit=0, reference_edit=False):
        fingerprint = revision(body)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT id, fingerprint FROM tasks WHERE owner=? AND action=? AND idem=?",
                             (owner, action, key)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise StoreConflict("idempotency_conflict")
                return old["id"]
            if company:
                started = db.execute("SELECT count(*) FROM tasks WHERE action=? AND json_extract(body,'$.mode')='company'",
                                     (action,)).fetchone()[0]
                if started >= company_limit:
                    raise StoreConflict("pilot_limit")
            if reference_edit:
                if body.get("mode") == "protected_sunburst":
                    previous = db.execute("""SELECT id,state,result FROM tasks WHERE owner=? AND code=? AND action='preview'
                        AND json_extract(body,'$.mode')='protected_sunburst'
                        AND json_extract(body,'$.reference_edit')=1
                        AND (state IN ('queued','running','unknown') OR json_extract(result,'$.background_runs.s01.status') IN ('unknown','pending'))
                        ORDER BY saved_at DESC LIMIT 1""", (owner, code)).fetchone()
                    if previous:
                        result = json.loads(previous["result"] or "{}")
                        run = result.get("background_runs", {}).get("s01", result.get("pending_call", {}))
                        body = {**body, "blocked_reference_call": {"job_id": previous["id"], "state": previous["state"],
                                **{key: run[key] for key in ("request_id", "provider_request_id", "task_id") if run.get(key)}}}
                started = db.execute("""SELECT count(*) FROM tasks WHERE action='preview' AND
                    json_extract(body,'$.reference_edit')=1""").fetchone()[0]
                body = {**body, "reference_edit": "blocked_reference_call" not in body and started < company_limit}
            active = db.execute("SELECT count(*) FROM tasks WHERE owner=? AND state IN ('queued','running')",
                                (owner,)).fetchone()[0]
            if active >= 4:
                raise StoreConflict("queue_full")
            task_id = uuid.uuid4().hex
            now = stamp()
            db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, NULL, NULL)",
                       (task_id, owner, code, action, key, fingerprint, now, now, canonical(body).decode()))
        return task_id

    def get(self, owner, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=? AND owner=?", (task_id, owner)).fetchone()
        return self.decode(row)

    def list(self, owner, code):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tasks WHERE owner=? AND code=? ORDER BY saved_at DESC LIMIT 30",
                              (owner, code)).fetchall()
        return [self.decode(row) for row in rows]

    def recoverable_preview(self, task):
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM tasks WHERE owner=? AND code=? AND action='preview'
                AND fingerprint=? AND state='failed' ORDER BY saved_at DESC LIMIT 8""",
                              (task["owner"], task["code"], task["fingerprint"])).fetchall()
        for row in rows:
            prior = self.decode(row)
            if (prior["error"] or {}).get("code") == "layout_failed" and (prior["result"] or {}).get("background_runs"):
                return prior
        return None

    def recoverable_plan(self, task):
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM tasks WHERE owner=? AND code=? AND action='plan'
                AND fingerprint=? AND state='failed' ORDER BY saved_at DESC LIMIT 8""",
                              (task["owner"], task["code"], task["fingerprint"])).fetchall()
        for row in rows:
            prior = self.decode(row)
            if (prior["error"] or {}).get("code") == "invalid_model_layout" and (prior["result"] or {}).get("call"):
                return prior
        return None

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("body", "result", "error"):
            if result[key] is not None:
                result[key] = json.loads(result[key])
        return result

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY saved_at LIMIT 1").fetchone()
            if row is not None:
                db.execute("UPDATE tasks SET state='running',updated_at=? WHERE id=?", (stamp(), row["id"]))
        return self.decode(row)

    def finish(self, task_id, state, result=None, error=None, *, from_state="running"):
        with self.connect() as db:
            db.execute("UPDATE tasks SET state=?,updated_at=?,result=?,error=? WHERE id=? AND state=?",
                       (state, stamp(), canonical(result) if result is not None else None,
                        canonical(error) if error is not None else None, task_id, from_state))

    def checkpoint(self, task_id, result):
        with self.connect() as db:
            db.execute("UPDATE tasks SET result=?,updated_at=? WHERE id=? AND state='running'",
                       (canonical(result), stamp(), task_id))

    def take_for_query(self, owner, task_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=? AND owner=? AND state='unknown'",
                             (task_id, owner)).fetchone()
            if row is not None:
                db.execute("UPDATE tasks SET state='running',updated_at=? WHERE id=?", (stamp(), task_id))
        return self.decode(row)

    def recover(self):
        # An interrupted task may already have submitted a provider request. Never replay it.
        with self.connect() as db:
            db.execute("UPDATE tasks SET state='unknown',updated_at=?,error=? WHERE state='running'",
                       (stamp(), canonical({"code": "interrupted", "message": "任务中断，须核对原任务状态。"})))
