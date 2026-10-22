# Usage and interpretation

[Back to the README](../README.md)

Run these commands from the repository root. Live collection requires macOS;
the synthetic demo and tests also work on other platforms.

## Run

Requires macOS and Python 3.11 or newer with curses. There are no third-party
runtime dependencies. From the repository directory:

```sh
python3 -m linger
```

Other views:

```sh
python3 -m linger --scope codex  # Focus on Codex and its descendants
python3 -m linger --once         # Load validated history, then sample once
python3 -m linger --json         # Structured snapshot (includes local paths/chat IDs)
python3 -m linger --demo         # Synthetic examples; works without macOS
python3 -m linger --demo --once
python3 -m linger --no-cache     # Observe in memory; no persistent cache reads/writes
```

Optionally use `uv run python -m linger`. `uv.lock` is included. The live collector
is macOS-only; the demo and tests can run on other platforms.

### Global command

With uv installed, run this from the repository root:

```sh
uv tool install . -e
```

Then use `linger` from any directory, for example `linger --demo --once` or
`linger --scope codex`. The editable installation uses this checkout directly;
source edits take effect without reinstalling. Keep the checkout in place, and
rerun the install command if you move it. Use `uv tool uninstall linger` to remove
the global command. The build uses setuptools; runtime dependencies remain empty.

Use `--codex-home PATH` to read a different Codex state directory; the default is
`~/.codex`. `--help` lists all arguments, and `--version` prints the version.

### Optional launch tracking

Launch a command under observation, optionally declaring its helpers temporary:

```sh
linger run --temporary -- python3 your_script.py
linger run -- python3 persistent_worker.py
```

This starts exactly the provided command without an implicit shell, inheriting
the current directory, environment, standard input/output, and foreground process
group. Use an explicit shell for pipelines. The wrapper waits for the command
and returns its exit code (or 128 plus the terminating signal number). Ctrl-C
reaches both through the terminal; Linger does not signal other processes. It
does not detach jobs or run a background service. Use another terminal's `linger`
to inspect the tracked processes.

`--temporary` declares that the command and its helpers should not persist after
the command ends. Without it, retained state is preserved. A directly launched
process is registered before execution; descendants are observed approximately
every 250 ms while the command runs, with native identity and parent checks.
Helpers that fork and reparent between observations can be missed. Observation
stops when the command exits; later descendants cannot be retroactively tracked.

An observed leftover from one complete temporary launch whose command exited
with code 0 receives `REVIEW` with a score floor of 35 and an explicit explanation.
This does not establish safe termination: memory state and external dependencies
are not inspected. Running owners, failed/interrupted commands, persistent
launches, incomplete records, and overlapping runs preserve uncertain state.
Codex, launchd, and existing infrastructure protections always take priority.
An absent wrapper or stale record never synthesizes successful completion.

Metadata lives in `~/Library/Application Support/Linger/launch-runs/`, using
private directories/files, atomic writes, and one record per run. It includes
run ID, UID, boot ID, precise PID/birth identities, temporary declaration, status,
exit code, update time, and coverage flag. Arguments, environment, executable
paths, and command output are not stored. At most 256 identities per run and
256 files are supported. Records older than seven days are ignored; files are
not automatically removed. At the file limit, starting a new tracked command
fails before executing it; archive or remove old records from Linger's directory
to make room. Another boot's records are rejected.

`--no-cache` in the monitor skips saved launch records as well as the Codex
ownership cache. Launch tracking itself requires metadata writes and accepts
only its own options, shown by `linger run --help`. JSON findings add `tracking`
and `service` metadata without changing the existing chat ownership fields.

## Review the evidence

| Key | Action |
| --- | --- |
| `j` / `k`, arrows | Select a row |
| `Tab` | Switch between processes and saved-chat evidence |
| `c` | Cycle all processes, Codex, and review candidates |
| `s` | Switch suspicion and memory sorting |
| `/` | Filter by PID, executable, class, chat context ID, or explanation; Enter accepts, Esc clears |
| `o` in saved chats | Show processes with observed associations to that chat |
| `h` | Expand or collapse the selected process's CPU/RSS history |
| `i` | Show or hide the evidence inspector, keeping the selected row |
| `[` / `]` | Scroll the selected row's explanation |
| `r` | Refresh now; otherwise refresh every five seconds |
| `g` / `G`, Page Up / Down | Navigate the list |
| `?` | Help and evidence limitations |
| `q` | Quit |

The default sort puts unprotected candidates with higher suspicion scores first.
Scores are rule weights, **not probabilities or permission to kill**. Summed RSS
includes shared pages and is not an estimate of reclaimable memory.

### Terminal layout and history

The default view has compact overall counts, a full-width process table, and a
short evidence inspector below the table. The inspector uses the classifier's
own score factors; `[` / `]` make the full explanations and process context
available beyond the compact summary. Filtering and scope change the table;
the header counts still describe the whole snapshot. Memory sorting keeps the
selected process selected. Page Up/Down move one visible page.

Amber marks review candidates. Mint marks `CODEX ACTIVE` processes whose
execution was observed in the latest registry sample. Lavender marks preserved
state and infrastructure. Muted `CODEX UNKNOWN` processes remain protected;
their ownership is unresolved. A `CODEX ACTIVE` label based only on saved-chat
lifecycle evidence stays lavender because current execution was not observed.

`CPU%` is the change in accumulated CPU time between two successful samples,
divided by elapsed monotonic time. It starts as `--`; 100% represents one core
and multithreaded processes may exceed 100%. The activity column shows a small
CPU sparkline when usage changes, otherwise measured idle time or `steady`.
An initial `sampling` value is not zero CPU usage.

History is collapsed initially. Press `h` to inspect the selected process's
CPU usage (%) and RSS samples from up to the last 60 seconds. Missing samples
are left unplotted. History keeps at most 120 points per current process
identity, discards exited processes, and resets CPU measurement after a long
sampling gap, a backwards CPU counter, or an identity change. Native birth time
is part of the identity when available. History is display-only and is never
saved in the ownership cache or used to infer chat ownership or abandonment.
The synthetic demo starts without prefabricated history too.

The layout targets 120 columns by 36 rows and also works down to 76 columns by
16 rows. Narrow layouts omit activity, then CPU%, then source columns to keep
names and classifications readable. The inspector appears with at least 22
rows, and expanded history requires the Processes view and at least 28 rows.
`i` hides the inspector to give the process table more space. Panels return
when there is room after a resize. Help is scrollable with `j` / `k`.

| Process class | Meaning |
| --- | --- |
| `REVIEW` | Generic suspicion or an observed leftover after successful temporary owner exit merits inspection; safe termination is unproven. |
| `OBSERVED` | Little suspicion evidence. |
| `CODEX CORE` | Codex app/agent infrastructure; preserve. |
| `CODEX ACTIVE` | An active registry execution is observed, or an associated saved chat has recent/active lifecycle evidence; preserve. |
| `CODEX SAVED` | Launch context or a remembered registry association references a saved chat, including chats outside the recent window; preserve possible tool state. |
| `CODEX MULTI` | The same native process has been associated with multiple chats; preserve every association. |
| `CODEX UNKNOWN` | Codex helper/descendant whose owner chat is unresolved; preserve possible active or saved-chat tool state. |
| `PROTECTED` | System/app/service infrastructure, another user's process, or Linger's caller ancestry. |
| `LAUNCHD` | Verified launchd job or a snapshot descendant; managed service, preserve. |
| `TRACKED` | Opt-in launch whose command has not reported exit; preserve. |
| `TRACK UNKNOWN` | Failed, interrupted, persistent, shared, or incomplete launch lifecycle; preserve. |
| `ZOMBIE` | Exited but unreaped. Signaling it cannot recover meaningful memory; the parent must reap it. |

The demo additionally shows a **synthetic** `LIKELY STALE` example to illustrate
a future detector with verified ephemeral-session ownership. **The live version
does not yet claim to identify completed ephemeral Codex helpers.** It does not
assign ownership from similar startup times, database absence, age, or low CPU.

## Codex: chat activity and tool state are different

The saved-chat view reads up to 32 recently updated entries from the local Codex
state database and the last 512 KiB of each corresponding rollout. Membership
checks cover **all saved chat IDs**, in one database read transaction. It displays
chat IDs and lifecycle evidence, not titles, prompts, or tool output:

- **ACTIVE:** a turn start was observed and chat events are less than 90 seconds old.
- **RECENT:** fresh events exist but the bounded tail has no usable turn boundary.
- **SAVED:** the last observed turn completed or aborted. Tool state may still be live.
- **UNKNOWN:** data is stale, missing, unrecognized, or inaccessible. Preserve state.

These are observations, not a live authoritative chat-status API. A waiting active
chat can become `UNKNOWN`, and disk writes may lag. Every saved chat is treated as
potentially owning in-memory tool state regardless of its last observed activity.
**A saved transcript does not imply its REPL variables or browser state survive
termination.**

### Direct launch context versus exact MCP ownership

For same-user Codex descendants, Linger now uses macOS native process APIs to
read `CODEX_THREAD_ID` and the legacy `CODEX_SESSION_ID` from the process's launch
environment. Native process birth time, parent, and UID are verified before and
after the read. Matching values provide a direct command-to-chat context; missing,
conflicting, malformed, inaccessible, or changed identities stay unknown.

This is **launch context, not proof of exclusive MCP ownership**. A command can
inherit its origin chat's environment. Shared Codex infrastructure is never
assigned an owner from an inherited context. A thread absent from the saved index
is not assumed to be ephemeral, completed, or disposable.

**The current Desktop MCP helpers examined here do not expose a thread ID in
their launch environment. Exact ephemeral-helper detection therefore remains
unavailable.** The UI reports observed associations and unresolved helpers.
Linger does not attach debuggers, inspect process memory, consume MCP pipes,
modify Codex, or infer ownership from timing.

### Registry observer

While the TUI runs, a read-only observer samples Codex's existing
`~/.codex/node_repl/active_execs/*.json` registry every 100 ms, independently of
the five-second process refresh. It validates the record schema, file ownership,
native process birth times, UID, and REPL/kernel parent relationship. It requires
no Codex hook, configuration change, service, or restart.

Associations are keyed by **PID and microsecond native birth time** and persisted
under `~/Library/Application Support/Linger/ownership-v1-<scope>.json`. When an
execution record disappears, the association is remembered;
it is invalidated when the process exits, becomes unreadable, or its identity
changes. An unchanged old record cannot attach its association to a reused PID.
A previously observed kernel stays protected if its parent later exits. Quitting
Linger stops the observer, saves revalidated history, and clears its memory cache.

The cache is loaded on first use and checkpointed at most once every five seconds,
plus on clean exit. It is private to your user (directory `0700`, files `0600`),
bound to the current macOS boot session and Codex home, and atomically replaced.
Concurrent Linger instances merge associations under a nonblocking file lock.
Every restored and saved entry is checked against live native PID, birth time,
and UID, then checked again before it appears in a scan. Dead, reused, unreadable,
or wrong-user processes lose their associations. A reboot or a different Codex
home cannot inherit them. Current execution activity is never persisted.

The cache contains only process identities, observed chat IDs, last-observation
times, an incomplete-history flag, and record identity anchors. It contains no
prompts, tool output, launch arguments, or environments. Unknown cache versions,
corruption, untrusted permissions, unavailable boot identity, or I/O failures
leave missing associations unknown and protected; scanning continues in memory.
The TUI shows cache status in its bottom status line and full diagnostics in
scrollable help. Disk history is protective evidence,
never permission to terminate a process.

Use `--no-cache` to skip all persistent cache reads and writes. It keeps the
in-memory observer for that run. The demo also never reads or creates the cache.
To forget persisted history, quit all Linger instances and remove the matching
`ownership-v1-<scope>.json` file in Linger's own directory.

The process table's `SOURCE` column shows `active` for a registry execution in the
latest sample, `cached` for a remembered association, `tracked` for opt-in launch
provenance, `launchd` for a verified managed service, `launch` for chat launch
context, or `-` for no association, in that priority order. Details and JSON expose all observed chat IDs, native
birth time, source, last registry observation, and current registry activity.
Activity expires if observation stops; remembered ownership remains protective.
Multiple observed chats are retained rather than presented as exclusive ownership.

`--once` and `--json` load revalidated cache history and poll once; earlier
executions that Linger never observed remain unknown. Library
users can run `with Scanner() as scanner:` to keep observation running across
scans, or `Scanner(persist=False)` for memory-only observation.
Helpers whose records disappeared before observation, or that never
execute a tool, remain unresolved. Missing, invalid, or conflicting evidence
keeps recognized Codex helpers protected. An unsaved chat ID never proves a
completed ephemeral session. See the [signal audit](read-only-signals.md)
for the discovery evidence and coverage limits.

See the [ownership investigation](ownership.md) for concrete evidence and
the missing producer-side integration.

## Scope and limitations

The core is generic: process snapshots, sampling, sorting, explanations, and UI.
Codex-specific evidence is isolated in a detector. Start with Codex; other
app-specific detectors can follow when there is evidence to support them.

Launchd parentage and an absent terminal are normal on macOS. CPU inactivity is
measured only while Linger is running, and cannot establish that a process has
finished its work. Known infrastructure is protected, but the list is not
exhaustive. Generic CPU sampling uses PID plus one-second process start time;
registry associations additionally require microsecond native birth time.
Snapshot races and missed short executions remain limitations. New Codex versions
may change local schemas; failure keeps ownership unknown and helpers protected.

The base collector runs `/bin/ps` without command arguments or environments and
opens Codex SQLite in read-only/query-only mode. The native context reader
temporarily reads the raw NUL-delimited launch arguments/environment of same-user
Codex descendants, extracting only the two allowlisted thread IDs. Raw launch
data is not logged, exported, or cached. Other values can include credentials and
remain internal to that transient read. Linger does not invoke Codex, create chats,
call model APIs, collect credentials, or send data over the network. JSON output can contain
private executable paths and chat IDs; inspect it before sharing.

See [evidence and implementation](evidence.md) for the boundaries of each
signal and the path toward stronger ownership detection.

## Develop

```sh
python3 -m unittest discover -s tests -v
python3 -m linger --demo --once
```

The tests cover process identity reuse (including within one second), native
argument/environment boundaries, conflicting IDs, activity boundaries,
missing/changed schemas, full saved-chat membership, external rollout paths,
terminal escapes, infrastructure protection, registry observation and cache
invalidation across restarts, private atomic persistence, concurrent writers,
boot/source isolation, file races, multiple chat associations, and the distinction between
saved history and possible live tool state.
