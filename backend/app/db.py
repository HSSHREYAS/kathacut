"""SQLite store: projects, jobs (the queue) and a free-quota usage log."""
import json
import sqlite3
import threading
import time
import uuid

from . import config

_lock = threading.RLock()
_conn = sqlite3.connect(str(config.DATA / "kathacut.db"), check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript(
    """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS projects(
        id TEXT PRIMARY KEY, kind TEXT, title TEXT, status TEXT,
        params TEXT, data TEXT, created REAL, updated REAL);
    CREATE TABLE IF NOT EXISTS jobs(
        id TEXT PRIMARY KEY, project_id TEXT, type TEXT, status TEXT,
        progress REAL, message TEXT, params TEXT, error TEXT,
        created REAL, updated REAL);
    CREATE TABLE IF NOT EXISTS usage(
        id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, provider TEXT,
        unit TEXT, amount REAL);
    """
)
_conn.commit()


def _q(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        rows = cur.fetchall()
        _conn.commit()
        return rows


def _row(r, json_cols=("params", "data")):
    if r is None:
        return None
    d = dict(r)
    for c in json_cols:
        if c in d:
            d[c] = json.loads(d[c]) if d[c] else {}
    return d


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---- projects
def create_project(kind, title, params) -> dict:
    pid, now = new_id(), time.time()
    _q("INSERT INTO projects VALUES(?,?,?,?,?,?,?,?)",
       (pid, kind, title, "new", json.dumps(params), "{}", now, now))
    (config.PROJECTS / pid).mkdir(parents=True, exist_ok=True)
    return get_project(pid)


def get_project(pid):
    rows = _q("SELECT * FROM projects WHERE id=?", (pid,))
    return _row(rows[0]) if rows else None


def list_projects():
    rows = _q("SELECT id,kind,title,status,created,updated FROM projects ORDER BY created DESC")
    return [dict(r) for r in rows]


def update_project(pid, **fields):
    sets, args = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        args.append(json.dumps(v) if k in ("params", "data") else v)
    sets.append("updated=?")
    args += [time.time(), pid]
    _q(f"UPDATE projects SET {','.join(sets)} WHERE id=?", args)


def patch_data(pid, **kv):
    """Merge keys into the project's data blob."""
    with _lock:
        p = get_project(pid)
        data = p["data"] or {}
        data.update(kv)
        update_project(pid, data=data)
        return data


def delete_project(pid):
    _q("DELETE FROM jobs WHERE project_id=?", (pid,))
    _q("DELETE FROM projects WHERE id=?", (pid,))


# ---- jobs
def enqueue(project_id, type_, params=None) -> dict:
    jid, now = new_id(), time.time()
    _q("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
       (jid, project_id, type_, "queued", 0.0, "Queued", json.dumps(params or {}), None, now, now))
    return get_job(jid)


def get_job(jid):
    rows = _q("SELECT * FROM jobs WHERE id=?", (jid,))
    return _row(rows[0], ("params",)) if rows else None


def jobs_for(project_id):
    rows = _q("SELECT * FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 30", (project_id,))
    return [_row(r, ("params",)) for r in rows]


def active_jobs():
    rows = _q("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created")
    return [_row(r, ("params",)) for r in rows]


def claim_job():
    with _lock:
        rows = _q("SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1")
        if not rows:
            return None
        jid = rows[0]["id"]
        _q("UPDATE jobs SET status='running', message='Starting', updated=? WHERE id=?",
           (time.time(), jid))
        return get_job(jid)


def update_job(jid, **fields):
    sets, args = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        args.append(v)
    sets.append("updated=?")
    args += [time.time(), jid]
    _q(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", args)


def requeue_interrupted():
    """Jobs left 'running' by a crash/restart go back to the queue."""
    _q("UPDATE jobs SET status='queued', message='Re-queued after restart' WHERE status='running'")


# ---- usage
def log_usage(provider, unit, amount=1.0):
    _q("INSERT INTO usage(day,provider,unit,amount) VALUES(?,?,?,?)",
       (time.strftime("%Y-%m-%d"), provider, unit, float(amount)))


def usage_today():
    rows = _q("SELECT provider,unit,SUM(amount) AS total FROM usage WHERE day=? GROUP BY provider,unit",
              (time.strftime("%Y-%m-%d"),))
    return [dict(r) for r in rows]
