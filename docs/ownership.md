# Ownership investigation and remaining integration

## Result

Direct process-to-chat **launch context** is available for some execution
children, via native `CODEX_THREAD_ID` / legacy `CODEX_SESSION_ID`. It is not
available in the launch environment of the Desktop MCP helpers examined here.
A subsequent read-only investigation found an existing **active execution
registry** that directly links an executing REPL and its kernel to a chat and
turn. That gives exact per-execution associations without a new diagnostic hook;
it does not establish exclusive lifetime ownership or identify never-used
ephemeral helpers. See [the read-only signal audit](read-only-signals.md).

Verified stale ephemeral-helper detection remains unresolved. No Codex settings
or binaries were modified. Registry validation used normal tool execution in this
chat and passive OS/file reads; it did not instrument other chats.

One live validation scan on 2026-10-21 bound 22 launch contexts, with 219
Codex-helper owners unresolved. The scan took about 0.28 seconds for 807
processes. All recognized Codex processes remained protected. Counts are an
observed snapshot, not fixed expectations or coverage guarantees.

## Observed facts

- Sampled MCP helper environments contain no `CODEX_THREAD_ID` or
  `CODEX_SESSION_ID`. Sampled execution children do contain a direct thread ID.
- MCP helpers are children of the same backend, with independent process groups.
  Those OS relationships cannot distinguish their owning chats.
- Browser executor IDs are app-instance/account/user identifiers. They are not
  chat IDs and must never be treated as ownership evidence.
- App tools receive thread metadata on tool requests; a helper's process
  environment does not thereby contain that metadata.
- The saved-thread index contains many more chats than the recent lifecycle
  view. Restricting membership checks to that view would misclassify old saved
  chats as missing. Membership now covers the full index in a read transaction.

The local packaged source inspected was
`/Applications/ChatGPT.app/Contents/Resources/app.asar`, member
`.vite/build/main-CLu1A25j.js`, SHA-256
`8aedee047ec0a078e65b24dc917c64ad544220395e773feb6740f4447008a495`.
Its `Ys` function constructs the browser executor ID from app-instance,
account, and user IDs. Static code establishes the intended identifier meaning;
it does not prove which chats a running helper currently serves.

REA tools were not connected in this session. Inspection used existing local
files, OS metadata, and upstream source; no REA Evidence IDs or runtime
instrumentation results are claimed. No extra analysis packages were installed.

## Corroborating upstream source

Upstream source was inspected at commit
`430a16799d90e296fb27b1e94f206023902b7e56`; it is corroboration, not proof that the
installed Desktop binary is identical:

- [Shell environment identity constants and injection](https://github.com/openai/codex/blob/430a16799d90e296fb27b1e94f206023902b7e56/codex-rs/protocol/src/shell_environment.rs).
- [Exec-server command metadata](https://github.com/openai/codex/blob/430a16799d90e296fb27b1e94f206023902b7e56/codex-rs/exec-server/README.md): thread metadata is exposed as `CODEX_THREAD_ID` for execution children.
- [MCP initialization](https://github.com/openai/codex/blob/430a16799d90e296fb27b1e94f206023902b7e56/codex-rs/codex-mcp/src/rmcp_client.rs): the initialization builder constructs protocol version, capabilities, and client identity; it does not attach a thread ID.
- [MCP status schema](https://github.com/openai/codex/blob/430a16799d90e296fb27b1e94f206023902b7e56/codex-rs/app-server-protocol/schema/typescript/v2/McpServerStatus.ts): no process identity or ownership mapping is exposed there.

## What is needed for exact ephemeral-helper ownership

The backend that creates and retains the MCP child process has the information
that an external process viewer lacks. A supported diagnostic producer should
expose:

1. Backend generation and native process identity (PID plus precise birth time).
2. Helper native identity, owning thread ID, and whether it is exclusive or shared.
3. Explicit ephemeral versus saved-session provenance at creation.
4. Explicit current session lifecycle, including completion and any later reuse.
5. An atomic, fresh view of all owners/leases; a saved or active owner overrides
   any completed ephemeral owner.

Only a fresh, identity-checked record with exclusively completed ephemeral
owners could justify `LIKELY STALE`. A missing saved-thread row, a completed
turn, low CPU, startup-time correlation, or an inherited environment variable
cannot supply those facts. A wrapper that waits for the first tool request is
also insufficient: unused helpers from background ephemeral generations may
never receive a tool call.

An existing producer does supply part of this evidence: the active REPL registry
described in the follow-up audit. Linger now observes that registry and remembers
validated associations by PID and native birth time. Coverage remains limited to
executions observed while Linger runs; further real-world coverage measurements
should precede any new hook. A future diagnostic
producer, if still needed for uncovered helpers, would require a Codex-side
integration/build outside Linger's current passive read-only scope. No
speculative ownership-log schema is accepted as authoritative input, and no
user-supplied assertion can turn a helper into a verified stale candidate.
