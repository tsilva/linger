# Read-only ownership signal audit

Investigation date: 2026-10-21. These are bounded observations of the installed
Desktop build and current processes, not a universal absence claim about every
Codex version. This audit preceded implementation; the registry observer is now
integrated as described in [evidence and implementation](evidence.md).

## Finding: an existing exact REPL execution association

The directory `~/.codex/node_repl/active_execs/` contains transient version-1 JSON
records with these fields:

```text
version, execId, sessionId, turnId, nodeReplPid, kernelPid, startedAtMs
```

The installed app validates this exact schema and uses matching `sessionId` and
`turnId` records when handling interrupted REPL executions. Static inspection
did not invoke that interrupt/termination code.

Live validation used normal REPL executions in the investigating chat. A
separate Python process read the existing registry and queried macOS native
process metadata. It confirmed:

- `sessionId` was the investigating chat's actual ID, rather than the unrelated
  random internal kernel session ID passed in `--session-id`.
- Both the REPL PID and kernel PID were alive, with stable native identities.
- The kernel's parent PID was the record's REPL PID.
- Both processes were born before the recorded execution started.
- The registry file belonged to the current user.
- The execution record disappeared on completion while both processes remained
  alive with the same native identities.

This is a direct **process/chat association during execution**, stronger than
startup-time correlation or working-directory similarity. It does not prove a
process can only ever serve that one chat.

The packaged consumer source is `app.asar` member
`.vite/build/src-DsLVGakC.js`, SHA-256
`28a0918f01c50b0eb17676e894998ad7182f119d978adf0467ccaac3583f259c`.
The schema is `OQ`; `kQ` enumerates records, `jQ` matches session/turn, and `NQ`
checks the kernel's process ancestry. The native REPL binary also contains the
`active_exec_registry.rs` source name and the registry-directory constant.
The existing registry was only read; it was never edited, removed, or populated
manually by the investigation.

## Other signals checked

| Signal | Observation | Ownership value |
| --- | --- | --- |
| Arguments and environment | Snapshot covered 140 `cua_node/bin/node*` helpers: no thread UUID in their arguments; browser executor/environment fields were shared across instances. | No additional chat binding in those fields. |
| Native REPL kernel session IDs | Four kernels had random 32-hex session IDs; no matches in the retained backend log store. | Internal kernel identity is not the chat identity. |
| Open rollout files | No sampled helper held a rollout file; one shared backend held 11. | Identifies backend activity, not which helper serves which chat. |
| Anonymous pipes and UNIX sockets | Helpers connect to shared backend/host infrastructure; socket/pipe handles expose connections, not chat IDs. | Exact process edges, insufficient chat semantics. |
| Browser and artifact endpoints | Safe endpoint metadata showed one shared app-tools/browser socket and one artifact host socket. | Endpoint identity is not chat ownership. |
| Working directories | Plugin helpers share plugin directories; other helpers share project directories. | Project grouping, not exact ownership. |
| Browser sidebar registry | 99 page entries had browser-tab/conversation/page fields, without a helper PID. | Useful browser-state protection, not helper ownership. |
| Browser session permission files | 64 recently modified session TOMLs had allowed-origin fields, without process identity fields. | Session configuration, not helper ownership. |
| Artifact session state | The inspected application-support tree was a shared runtime generation; the temporary host directory exposed readiness state, not a per-helper/chat map. | No usable ownership join found in those locations. |
| Other SQLite state | Queue/history/goal schemas carry chat/turn IDs, without MCP-helper PID fields. | Lifecycle evidence, not the missing process join. |
| Existing logs | 40 recent Desktop log files (46.5 MiB) had no operational PID/chat record matching the tested field patterns; the retained backend store held 32,267 records when inspected. | No join found within this retention/search boundary. |
| Existing debugging endpoint | No debugging switches on the main Desktop process and no TCP listener on that process or its main backend. | No already-enabled passive CDP route found; none was enabled. |
| App coordination IPC | Static method inventory includes thread-owner discovery and follower coordination. Those describe app/client ownership; no helper-PID diagnostic was found in the inspected interface. | Does not establish MCP-child ownership. The live IPC socket was not connected to. |

Ordinary process metadata such as PID, parent, process group, UID, and native
birth time identifies processes, not application-level chats. Pipe metadata in
the installed macOS SDK similarly exposes handles, peers, status, and file
metadata rather than a Codex chat identity. No pipe payload was read, duplicated,
or consumed, and no debugger or process-memory reader was attached.

## Coverage limits and next step

Use the existing registry before adding a Codex diagnostic hook:

1. Observe it while REPL executions occur and validate record shape, current-user
   ownership, process birth times, and kernel/REPL ancestry.
2. Retain **observed associations** in memory keyed by PID plus precise birth
   time, including multiple chat IDs if ever seen. Revalidate identities on each
   scan and discard bindings when a process exits or its identity changes.
3. Treat registry activity as a protection signal. Preserve known saved-chat
   state after the record disappears. An empty registry does not mean a helper
   is stale or unowned.
4. Measure coverage per helper type. Only the current chat's native REPL was
   validated live; browser/artifact REPLs using the same native binary are a
   plausible extension to verify, not already-proven coverage.

The files exist only during execution. A five-second TUI refresh can miss short
executions; a watcher can also miss a record that disappears before it is read.
Starting Linger after a helper becomes idle does not recover deleted records.
All such gaps must remain unknown.

This does **not** yet solve stale prediction/title-generation helper cleanup.
Those helpers may never execute a REPL tool and therefore never publish a
record. Code-review, security, and app-tools servers are also not assigned the
REPL's owner merely because they started near it. Even a direct REPL association
does not supply explicit ephemeral provenance, complete lifetime ownership, or
session completion. A hook may still be needed for that remaining coverage, but
the previous blanket suggestion that read-only ownership requires a new hook
was too broad.

## Scope preserved

No Codex settings, binaries, configuration, or other chats were changed. No
target process was stopped. Existing files and native OS metadata were read;
normal tool calls in this chat were used to validate the transient registry.
No analysis packages were installed: REA was not connected, and its CLI was
unavailable in a no-install check. No REA Evidence IDs are claimed. Raw process
environments and credentials are not retained in the investigation report or
scratch metadata; unrelated browser history/cookie/credential databases were
not read.
