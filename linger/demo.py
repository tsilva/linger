"""Synthetic examples only. No local process or chat data is accessed."""

import time

from .models import Chat, Finding, Process, Snapshot


def snapshot() -> Snapshot:
    now = time.time()

    def proc(pid, name, rss=120000):
        return Process(pid, 1, 501, rss, now - 7200, 0.2, "S", "??", name, 120, True)

    return Snapshot(now, [
        Finding(proc(4812, "/demo/ephemeral-helper"), "LIKELY STALE", 85,
                ("SYNTHETIC: verified owner was an ephemeral session that completed.", "SYNTHETIC: identity unchanged; no newer activity or saved-chat ownership.", "Illustrates a future direct-ownership detector; live Codex detection cannot establish this yet.")),
        Finding(proc(4901, "/demo/old-worker"), "REVIEW", 50,
                ("Parent is launchd; could be orphaned or a managed service.", "No CPU increase observed for 120s; this does not prove abandonment.")),
        Finding(proc(5001, "/demo/codex-helper"), "CODEX UNKNOWN", 0,
                ("Owner chat cannot be verified.", "May hold active-chat or saved-chat tool state; preserve."), codex=True, protected=True),
        Finding(proc(5002, "/demo/codex"), "CODEX CORE", 0,
                ("Shared infrastructure may serve active chats; preserve.",), codex=True, protected=True),
    ], [
        Chat("00000000-0000-0000-0000-000000000001", "ACTIVE", "Observed turn start and fresh activity; preserve.", now),
        Chat("00000000-0000-0000-0000-000000000002", "SAVED", "Last observed turn ended; possible REPL/browser state persists.", now - 300),
        Chat("00000000-0000-0000-0000-000000000003", "UNKNOWN", "No current lifecycle evidence; preserve.", now - 1000),
    ], ["DEMO: synthetic candidates and chats. Live ownership is not inferred from timing."], demo=True)
