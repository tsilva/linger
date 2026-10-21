"""Read saved-chat metadata and lifecycle evidence without contacting Codex.

No write connections, chat content output, CLI invocation, or API sessions.
The current detector intentionally does not assign helper ownership by timing.
"""

import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import time

from .models import Chat

UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
TAIL_BYTES = 512 * 1024
MAX_CHATS = 32


def lifecycle(data: bytes, now: float) -> tuple[str, str, float]:
    latest_event = 0.0
    last_boundary: tuple[str, float] | None = None
    for raw in data.splitlines():
        try:
            item = json.loads(raw)
            if item.get("type") != "event_msg":
                continue
            timestamp = dt.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).timestamp()
            kind = item["payload"].get("type")
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        latest_event = max(latest_event, timestamp)
        if kind in {"task_started", "task_complete", "turn_aborted"}:
            last_boundary = (kind, timestamp)
    # A stale log is not an authoritative statement that a turn is still live.
    fresh = 0 <= now - latest_event <= 90
    if last_boundary and last_boundary[0] == "task_started" and fresh:
        return "ACTIVE", "Observed turn start with recent event activity; may lag live state.", latest_event
    if last_boundary and last_boundary[0] in {"task_complete", "turn_aborted"}:
        return "SAVED", "Last observed turn ended. Saved chat may retain live REPL/browser tool state.", latest_event
    if fresh:
        return "RECENT", "Recent chat events; turn boundary missing from bounded tail. Preserve state.", latest_event
    return "UNKNOWN", "Saved chat exists, but current activity cannot be established. Preserve state.", latest_event


class CodexReader:
    def __init__(self, home: Path | None = None):
        self.home = home or Path.home() / ".codex"
        self.cache: dict[str, tuple[int, int, bytes]] = {}
        # Membership must cover ALL saved chats, not only the UI's recent 32.
        # None distinguishes unreadable/incomplete membership from an empty DB.
        self.saved_ids: set[str] | None = None

    def read(self, now: float | None = None) -> tuple[list[Chat], list[str]]:
        now = time.time() if now is None else now
        self.saved_ids = None
        warnings: list[str] = []
        # Schema numbers are data versions, not timestamps. Ignore WAL/SHM.
        databases = list(self.home.glob("state_*.sqlite"))
        databases.sort(key=lambda p: int(p.stem.removeprefix("state_")) if p.stem.removeprefix("state_").isdigit() else -1, reverse=True)
        if not databases:
            return [], ["Codex saved-thread database unavailable; helper ownership remains unknown."]
        connection = None
        try:
            connection = sqlite3.connect(databases[0].resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            ids = connection.execute("SELECT id FROM threads").fetchall()
            if any(not isinstance(row[0], str) or not UUID.fullmatch(row[0]) for row in ids):
                raise sqlite3.DatabaseError("Invalid saved-thread identity")
            saved_ids = {row[0].lower() for row in ids}
            rows = connection.execute(
                "SELECT id, rollout_path, updated_at FROM threads ORDER BY updated_at DESC LIMIT ?",
                (MAX_CHATS,),
            ).fetchall()
            self.saved_ids = saved_ids
        except sqlite3.Error:
            return [], ["Codex database unreadable or schema changed; all helpers remain protected."]
        finally:
            if connection is not None:
                connection.close()
        chats = []
        next_cache = {}
        for thread_id, rollout_path, updated in rows:
            if not isinstance(thread_id, str) or not UUID.fullmatch(thread_id):
                continue
            status, evidence, event_time = "UNKNOWN", "Rollout unavailable; preserve possible tool state.", 0.0
            try:
                path = Path(rollout_path).resolve()
                # Never follow arbitrary paths from the SQLite index. Only saved
                # Codex rollouts in the configured home may be inspected.
                if not any(path.is_relative_to((self.home / folder).resolve()) for folder in ("sessions", "archived_sessions")):
                    raise OSError("rollout outside trusted roots")
                stat = path.stat()
                cached = self.cache.get(str(path))
                if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                    data = cached[2]
                else:
                    with path.open("rb") as f:
                        f.seek(max(0, stat.st_size - TAIL_BYTES))
                        data = f.read(TAIL_BYTES)
                next_cache[str(path)] = (stat.st_mtime_ns, stat.st_size, data)
                status, evidence, event_time = lifecycle(data, now)
            except (OSError, ValueError, TypeError):
                pass
            chats.append(Chat(thread_id, status, evidence, event_time or updated))
        self.cache = next_cache
        warnings.append(f"Lifecycle tails: {len(chats)} recent saved chats; membership: all {len(self.saved_ids)} saved chats.")
        return sorted(chats, key=lambda c: ({"ACTIVE": 0, "RECENT": 1, "SAVED": 2, "UNKNOWN": 3}[c.status], -c.updated)), warnings
