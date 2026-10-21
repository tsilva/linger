"""Bind a native launch context to saved-thread evidence, without guessing.

An execution context establishes where a command originated. It does NOT prove
exclusive MCP-server ownership, nor that a missing thread is ephemeral.
"""

from dataclasses import replace

from .models import Chat, Finding
from .native import Context
from .registry import RegistryBinding


def bind(findings: list[Finding], contexts: dict, chats: list[Chat],
         saved_ids: set[str] | None, registry: dict | None = None) -> tuple[list[Finding], list[str]]:
    states = {c.id.lower(): c for c in chats}
    result = []
    for f in findings:
        if not f.codex:
            result.append(f)
            continue
        context: Context | None = contexts.get(f.process.identity)
        # Shared app/backend infrastructure never acquires a chat owner merely
        # because it inherited the shell that originally launched the app.
        if f.label == "CODEX CORE":
            result.append(f)
            continue
        if context is None or not context.readable:
            reason = "Native launch context inaccessible or process identity changed; preserve."
        elif context.conflict:
            reason = "Conflicting or malformed thread IDs in native launch context; preserve."
        elif not context.threads:
            reason = "No direct thread ID in this helper's launch context; timing is not ownership."
        else:
            thread = context.threads[0]
            chat = states.get(thread)
            reasons = [
                "Direct CODEX_THREAD_ID/CODEX_SESSION_ID launch context, with native process birth rechecked.",
                "This identifies command origin; it does not establish exclusive MCP ownership.",
            ]
            if saved_ids is None:
                label = "CODEX UNKNOWN"
                reasons.append("Saved-thread index unavailable; preserve possible saved-chat tool state.")
            elif thread in saved_ids:
                label = "CODEX ACTIVE" if chat and chat.status in {"ACTIVE", "RECENT"} else "CODEX SAVED"
                reasons.append("Saved chat: preserve REPL/browser/command state even after its turn ends.")
                if chat:
                    reasons.append(chat.evidence)
                else:
                    reasons.append("Saved owner is outside the recent-chat window; current activity is unknown.")
            else:
                label = "CODEX UNKNOWN"
                reasons.append("Thread absent from saved index. This does not prove it is ephemeral or completed.")
            result.append(replace(f, label=label, owner=thread, score=0,
                                  reasons=tuple(reasons), protected=True,
                                  ownership_kind="launch context", birth_us=context.birth_us,
                                  owners=(thread,)))
            continue
        result.append(replace(f, reasons=(*f.reasons, reason)))

    # Registry associations are independent of launch environments. A remembered
    # association stays useful after the transient execution file disappears.
    # Never propagate it to siblings, shared app infrastructure, or a new birth.
    combined = []
    for f in result:
        binding: RegistryBinding | None = (registry or {}).get(f.process.identity)
        if f.label == "CODEX CORE" or binding is None:
            combined.append(f)
            continue
        context = contexts.get(f.process.identity)
        if context is not None and context.birth_us != binding.birth_us:
            combined.append(replace(f, label="CODEX UNKNOWN", owner="unresolved", owners=(),
                                    ownership_kind="unknown", birth_us=None, protected=True, score=0,
                                    reasons=("Process identity changed between evidence reads; chat association unknown, preserve.",)))
            continue
        owners = set(binding.owners) | set(f.owners)
        conflict = context is not None and context.conflict
        if conflict:
            owners.update(context.threads)
        owners = tuple(sorted(owners))
        if not owners:
            combined.append(f)
            continue
        active = bool(binding.active_owners)
        reasons = [
            "Registry directly associated this process with a chat execution; native birth and ancestry were checked.",
            "Execution observed in the latest registry sample." if active else
            "Remembered registry association; no current execution record. Missing records do not imply stale state.",
            "Associations may include multiple chats; exclusive lifetime ownership is not established.",
        ]
        if conflict or binding.incomplete:
            label = "CODEX UNKNOWN"
            reasons.append("Conflicting launch context or incomplete association history; preserve all possible state.")
        elif len(owners) > 1:
            label = "CODEX MULTI"
            reasons.append("Multiple chat associations observed; preserve shared or reused tool state.")
        elif active:
            label = "CODEX ACTIVE"
        elif saved_ids is None:
            label = "CODEX UNKNOWN"
            reasons.append("Saved-thread membership unavailable; preserve possible saved-chat tool state.")
        elif owners[0] in saved_ids:
            chat = states.get(owners[0])
            label = "CODEX ACTIVE" if chat and chat.status in {"ACTIVE", "RECENT"} else "CODEX SAVED"
            reasons.append("Saved chat: preserve REPL/browser/command state after the execution record disappears.")
        else:
            label = "CODEX UNKNOWN"
            reasons.append("Observed chat absent from saved index. This does not prove ephemeral provenance or completion.")
        source = "registry active" if active else "registry remembered"
        if f.ownership_kind == "launch context":
            source += " + launch context"
        combined.append(replace(f, label=label, codex=True, owner=owners[0] if len(owners) == 1 else "multiple",
                                owners=owners, ownership_kind=source, birth_us=binding.birth_us,
                                registry_active=active, registry_observed_at=binding.last_seen,
                                score=0, protected=True, reasons=tuple(reasons)))
    bound = sum(bool(f.owners) for f in combined if f.codex)
    registry_count = sum(f.registry_observed_at is not None for f in combined)
    active_count = sum(f.registry_active for f in combined)
    unknown = sum(not f.owners for f in combined if f.codex and f.label != "CODEX CORE")
    warnings = [f"{bound} associated; registry {active_count} active/{registry_count-active_count} remembered; {unknown} unresolved helpers. All Codex state protected."]
    return combined, warnings
