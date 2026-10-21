# Evidence and implementation

## Current inputs

- `/bin/ps -axww` fields: PID, parent PID, UID, RSS, start time, accumulated CPU
  time, kernel state, terminal, and executable path (`comm`, not `command`).
- `~/.codex/state_*.sqlite`: newest numeric schema version, opened with
  `mode=ro` and `PRAGMA query_only=ON`. All saved IDs and the 32 most recently
  updated saved threads' `id`, `rollout_path`, and `updated_at` are selected in
  one transaction. Old and archived saved chats retain membership protection.
- Native `proc_pidinfo(PROC_PIDTBSDINFO)` before/after `sysctl(KERN_PROCARGS2)`
  for same-user Codex descendants. Only valid `CODEX_THREAD_ID` and
  `CODEX_SESSION_ID` UUIDs survive the read. Birth time is checked at microsecond
  resolution together with UID and parent PID. Raw arguments and other
  environment values never enter snapshots, caches, or error messages.
- Saved rollouts underneath that Codex home's `sessions` or `archived_sessions`:
  at most 512 KiB each. Only `event_msg` timestamps and the lifecycle event types
  `task_started`, `task_complete`, and `turn_aborted` influence activity labels.
  Arbitrary external paths and symlink escapes are rejected.
- `~/.codex/node_repl/active_execs/*.json`: existing version-1 REPL execution
  records, sampled read-only every 100 ms while the TUI runs. One-shot modes
  sample once. No registry files or Codex configuration are written.
- Linger's `~/Library/Application Support/Linger/ownership-v1-<scope>.json`:
  bounded version-1 ownership history. This is Linger's own output, never a
  Codex file. `--no-cache` skips both loading and saving it.

No process arguments, environments, chat titles, or message text are emitted.
The JSON decoder temporarily reads the bounded rollout data in memory to select
event metadata; this is not a claim that raw chat bytes are never read. Cached
tails stay in memory for refreshes and are discarded when Linger exits.

## Suspicion rules

Protections take priority over resource use. Codex lineage, system/other-user
processes, application bundles, Linger's own ancestors, and known shell/service
lineages are protected from stale recommendations.

For unprotected processes:

| Evidence | Weight | Limitation |
| --- | ---: | --- |
| Parent missing from snapshot | 40 | May be an enumeration race. |
| Parent is PID 1 | 20 | May be a legitimate launchd service. |
| No controlling terminal | 5 | Normal for background workers. |
| Age at least 30 minutes | 10 | Age does not establish idleness. |
| No CPU-time increment observed for at least 60 seconds | 15 | Waiting work can be legitimate. |

Scores of 35 or more are `REVIEW`, never a verified stale determination. Kernel
`Z` state is shown separately with score 90 to draw attention to an unreaped
process, with an explanation that it has already exited. It is not a reclaimable
helper. A future UI may separate this into its own diagnostic group.

## Ownership evidence and remaining gaps

Direct launch-context IDs bind commands to their originating chat when exposed;
this is not proof of exclusive MCP-server ownership. Helpers with a live Codex
ancestor or an executable inside a Codex/ChatGPT app bundle are recognized as
Codex-owned. The active REPL registry supplies direct per-execution chat
associations for a REPL and its kernel. A previously observed kernel retains its
Codex protection even after losing its parent. A helper never observed in the
registry that loses both its ancestry and recognizable executable path may
escape this detector; generic review never establishes permission to terminate it.

Registry directories and files must belong to the current user and must not be
group/world writable. Symlinks and non-regular files are rejected. Records are
limited to 8 KiB, duplicate JSON keys are rejected, and the schema and timestamp
are checked. Native birth time, UID, and the direct kernel/REPL relationship are
validated before and after reading. Inode, size, modification time, and change
time must remain stable through validation. Process births cannot postdate the
reported execution start.

The ownership cache uses `(PID, native birth time in microseconds)` and retains
observed chat associations across restarts. Each current record is anchored to the first
validated native identities so an unchanged old file cannot rebind to a reused
PID. Dead, unreadable, reused, or wrong-user identities invalidate cached
entries. Associations are projected only onto their own processes, never
siblings or shared core infrastructure.

Disappearing records clear current-execution flags while retaining historical
associations. Current activity also expires when polling is more than one second
old. Saved-chat associations remain protected; missing saved membership never
proves ephemeral provenance. Multiple chats produce `CODEX MULTI`; conflicts or
incomplete ownership produce `CODEX UNKNOWN`. All remain protected with score 0.

Observation is bounded to 1,024 JSON records and 4,096 directory entries per poll,
2,048 cached native identities, and 64 chat associations per identity. Exceeding
these bounds reduces coverage, reports a warning, and leaves missing associations
unknown. Closing the observer stops its thread and clears its memory cache.

## Persistent ownership history

`cache.py` stores only native process identities, observed chat UUIDs, last-seen
timestamps, incomplete-history flags, and execution-record anchors. It excludes
active flags, arguments/environments, rollout bytes, titles, and tool state.
Anchors survive restarts so an unchanged old execution file cannot rebind to a
reused PID. The envelope includes schema version, UID, macOS `kern.bootsessionuuid`,
and a SHA-256 scope of the resolved Codex home. Boot/source/schema mismatches
yield no restored evidence. They can be rebuilt from fresh validated observations.

Every loaded entry is revalidated against native PID, microsecond birth time,
and UID before import, and again before projection onto a process snapshot.
Activity always starts empty on restore. Saved-chat membership and lifecycle are
read afresh; neither a cached owner nor its age establishes current activity or
completed ephemeral provenance. Cache entries only add protection.

The private directory must be owned by the current user with no group/other
permissions; files must also be private, regular, owned, and have one hard link.
Final directory/file symlinks and FIFOs are rejected. The store refuses a path
inside Codex home. Reads are bounded to 8 MiB and reject duplicate JSON fields,
invalid UUIDs, nonfinite/future timestamps, excessive entries/owners/anchors,
and incompatible schemas. Errors report fixed metadata-only messages.

The observer checkpoints every five seconds and on close, revalidating entries
before saving. A nonblocking `flock` protects read/merge/write so concurrent
Linger instances retain the union of known chats rather than overwrite each
other. Merging over the owner limit marks history incomplete and protected.
Writes use a private exclusive temporary file, flush/fsync its contents, then
atomically replace Linger's own JSON file. Failed writes clean their temporary
file and preserve the previous snapshot; a busy lock or I/O failure leaves
in-memory observation running. A crash may lose observations since the last
checkpoint. `--no-cache`, the demo, and an unused scanner create no cache files.

Any future `LIKELY STALE` classification for a Codex helper needs independently
verified ownership, an explicit ephemeral-session lifecycle, and evidence that
the owner completed. Absence from the saved-thread index is insufficient: a chat
can be temporarily missing or not persisted yet. Startup-time correlation is
insufficient: multiple active and ephemeral sessions can start together.

Active chats, saved-chat tool state, conflicting evidence, inaccessible data,
and unrecognized schemas must override stale recommendations. A future action
feature requires a separate design, explicit user authorization, and fresh
identity/ownership/lifecycle checks immediately before each action. The current
code contains no such action feature.

## Module boundaries

- `processes.py`: platform collection and CPU-inactivity sampling.
- `codex.py`: bounded, local saved-chat lifecycle evidence.
- `native.py`: native process identity and allowlisted launch-context extraction.
- `registry.py`: read-only execution observation and native-identity association cache.
- `cache.py`: private, boot/source-scoped ownership persistence and concurrent merging.
- `ownership.py`: combine launch/registry evidence with full saved membership; preserve uncertainty.
- `scoring.py`: conservative process classification with reasons.
- `scan.py`: combine independent evidence into a snapshot.
- `tui.py`: presentation, view controls, and background refresh.
- `demo.py`: synthetic, clearly marked future and current examples.

Live behavior was developed against the locally observed Codex `state_5.sqlite`
schema and rollout event types on macOS on 2026-10-21. These are internal formats,
not a stable public integration contract.
