"""Explainable suspicion, not a probability of safe termination."""

import os
from pathlib import PurePosixPath

from .models import Finding, Process
from .processes import is_codex_executable, own_ancestry

INFRASTRUCTURE = {
    "sshd", "ssh", "tmux", "screen", "zsh", "bash", "fish", "sh",
    "postgres", "mysqld", "redis-server", "mongod", "nginx", "caddy", "httpd",
    "docker", "dockerd", "containerd", "ollama",
}


def classify(rows: list[Process], now: float, uid: int | None = None) -> list[Finding]:
    uid = os.getuid() if uid is None else uid
    by_pid = {p.pid: p for p in rows}
    ancestors = own_ancestry(rows)

    def chain(p: Process) -> list[Process]:
        result, seen = [], set()
        while p.pid not in seen:
            result.append(p)
            seen.add(p.pid)
            if p.ppid not in by_pid:
                break
            p = by_pid[p.ppid]
        return result

    findings = []
    for p in rows:
        lineage = chain(p)
        codex = any(is_codex_executable(a.executable) for a in lineage)
        basename = PurePosixPath(p.executable).name
        age = max(0, now - p.started)
        if codex:
            if is_codex_executable(p.executable) and basename not in {"node", "node_repl", "python", "python3"}:
                reasons = ("Codex application/agent infrastructure; may serve active chats.", "No authoritative per-process chat ownership; preserve.")
                label = "CODEX CORE"
            else:
                reasons = ("Codex descendant/helper; owner chat cannot be verified.", "May hold active-chat or saved-chat REPL/browser state.", "Age and low CPU do not establish ephemeral-session completion.")
                label = "CODEX UNKNOWN"
            findings.append(Finding(p, label, 0, reasons, codex=True, protected=True))
            continue
        protected_reason = None
        if p.pid in ancestors:
            protected_reason = "Linger or its caller ancestry."
        elif p.uid != uid or p.pid <= 1:
            protected_reason = "System process or a different user."
        elif ".app/" in p.executable or p.executable.startswith(("/System/", "/usr/libexec/", "/sbin/", "/usr/sbin/")):
            protected_reason = "Application bundle or platform infrastructure."
        elif any(PurePosixPath(a.executable).name in INFRASTRUCTURE for a in lineage):
            protected_reason = "Shell, connection, multiplexer, service, or its descendant."
        if protected_reason:
            findings.append(Finding(p, "PROTECTED", 0, (protected_reason, "Resource use alone is not abandonment evidence."), protected=True))
            continue
        if "Z" in p.state:
            findings.append(Finding(p, "ZOMBIE", 90, ("Kernel reports an exited, unreaped process (Z).", "Already exited: signaling it will not reclaim meaningful memory; parent must reap it.")))
            continue
        score, reasons = 0, []
        if p.ppid == 1:
            score += 20
            reasons.append("Parent is launchd (PID 1): could be orphaned OR a legitimate managed service.")
        elif p.ppid not in by_pid:
            score += 40
            reasons.append("Parent absent from this snapshot; could also be a collection race.")
        if p.tty in {"??", "?", "-"}:
            score += 5
            reasons.append("No controlling terminal; normal for background services.")
        if age >= 1800:
            score += 10
            reasons.append("Running for at least 30 minutes (age is not idle time).")
        if p.sampled and p.observed_idle >= 60:
            score += 15
            reasons.append(f"No measurable CPU-time increase over {p.observed_idle:.0f}s observed by Linger; waiting work can be legitimate.")
        elif not p.sampled:
            reasons.append("First sample: CPU inactivity has not been measured yet.")
        # Abandonment requires independent ownership/lifecycle evidence. Generic
        # ps facts cannot prove it, so this detector offers REVIEW, never STALE.
        label = "REVIEW" if score >= 35 else "OBSERVED"
        reasons.append("No lifecycle evidence of abandonment; manual review only.")
        findings.append(Finding(p, label, score, tuple(reasons)))
    return sorted(findings, key=lambda f: (f.protected, -f.score, -f.process.rss_kib, f.process.pid))
