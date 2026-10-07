# k3code TUI Gateway Contract Map

This document maps every JSON-RPC method and server→client event in the vendored Hermes TUI (`tui/`) to the gateway contract in `tui_gateway/contracts/*.py`. The contract is the single source of truth; this map is for k3code implementers.

**Source:** Hermes Agent commit `4127d78da84b1eee105f298979cc57cc7457f98d`  
**Generated from:** `tui_gateway/contracts/` (Python Pydantic models) → `tui/shared/gateway-contract.generated.ts`  
**TUI calls ~40 methods** of the ~121 registered; the rest are desktop/dashboard-only.

---

## Classification Legend

| Class | Meaning |
|-------|---------|
| **M1-core** | Needed for chat, streaming, tools, approvals, sessions, model, config, slash commands, sub-agent/agents panel |
| **later** | Goals, loops, cron, and other features k3code will add later |
| **cut** | Nous-specific features to remove or stub: billing, free_tier, subscription, connectors, vault, voice, wake, bot_relay, hosted rooms, pets |

---

## Client → Server Methods (RPC)

### M1-core (33 methods)

| Method | Params | Result | Used by TUI | Notes |
|--------|--------|--------|-------------|-------|
| `session.create` | `{profile?, cwd?, model?, goal?, tools?, compact?}` | `{session_id, info: SessionLiveInfo}` | ✅ | New chat session |
| `session.list` | `{limit?, offset?, include_hidden?}` | `{sessions: SessionListItem[]}` | ✅ | Session picker/history |
| `session.active_list` | `{current_session_id?}` | `{sessions: ActiveSession[]}` | ✅ | Agent strip / switcher |
| `session.resume` | `{session_id, compact?}` | `{session_id, info: SessionLiveInfo}` | ✅ | Resume stored session |
| `session.activate` | `{session_id}` | `{session_id, info: SessionLiveInfo}` | ✅ | Switch active session |
| `session.delete` | `{session_id}` | `{deleted: boolean}` | ✅ | Delete session |
| `session.title` | `{session_id, title}` | `{ok: boolean}` | | Auto-titling (server sends `session.title` event) |
| `session.interrupt` | `{session_id}` | `{interrupted: boolean}` | ✅ | Stop current turn |
| `session.steer` | `{session_id, text}` | `{steered: boolean}` | ✅ | Inject user text mid-turn |
| `session.control.read` | `{session_id}` | `{control: SessionControlSnapshot}` | ✅ | Read goal/loop/heartbeat |
| `session.control` | `{session_id, goal?, loop?, heartbeat?}` | `{ok: boolean}` | | Write goal/loop/heartbeat |
| `session.workspace.move` | `{session_id, path}` | `{ok: boolean}` | | Move session cwd |
| `session.branch_stored` | `{session_id, branch_name?}` | `{session_id}` | | Fork stored session |
| `session.most_recent` | `{}` | `{session_id?, info?}` | | Last session |
| `session.events.since` | `{session_id, seq}` | `{events: GatewayEvent[]}` | | Event replay |
| `session.events.stats` | `{session_id}` | `{total, by_type}` | | Event stats |
| `prompt.submit` | `{session_id, text, attachments?, images?, files?, model?, tools?, compact?}` | `{turn_id}` | ✅ | Send user message |
| `clipboard.paste` | `{session_id}` | `{text?, images?, files?}` | ✅ | Paste from clipboard |
| `image.attach` | `{session_id, data_uri, name?}` | `{id}` | ✅ | Attach image |
| `image.attach_bytes` | `{session_id, base64, mime, name?}` | `{id}` | | Attach image from bytes |
| `image.detach` | `{session_id, path}` | `{ok: boolean}` | ✅ | Detach image |
| `file.attach` | `{session_id, path, name?}` | `{id}` | | Attach file |
| `pdf.attach` | `{session_id, data_uri, name?}` | `{id}` | | Attach PDF |
| `input.detect_drop` | `{session_id, paths[]}` | `{images[], files[]}` | ✅ | Detect dropped files |
| `command.dispatch` | `{name, arg?, session_id}` | `CommandResult` | ✅ | Slash commands |
| `slash.exec` | `{command, session_id}` | `SlashExecResponse` | ✅ | Internal slash exec |
| `model.options` | `{provider?, profile?}` | `ModelOptionsResult` | ✅ | Model picker |
| `model.save_key` | `{provider, key, profile?}` | `{provider?}` | ✅ | Save API key |
| `model.disconnect` | `{provider, profile?}` | `{disconnected?}` | ✅ | Remove API key |
| `config.get` | `{key: "full" \| "mtime" \| ...}` | `ConfigFullResponse \| ConfigMtimeResponse` | ✅ | Config polling |
| `config.set` | `{key, value, profile?}` | `{ok: boolean}` | | Write config |
| `setup.status` | `{}` | `{provider_configured, free_tier, ...}` | | Setup status |
| `system.battery` | `{}` | `{percent?, charging?, ...}` | ✅ | Battery status |

### Later (12 methods)

| Method | Params | Result | Notes |
|--------|--------|--------|-------|
| `session.compress` | `{session_id, target_tokens?}` | `{compressed: boolean}` | Context compaction |
| `session.close` | `{session_id}` | `{closed: boolean}` | Close without delete |
| `delegation.status` | `{}` | `{active[], paused, max_depth, max_concurrent}` | Sub-agent panel |
| `delegation.pause` | `{paused}` | `{paused: boolean}` | Pause delegation |
| `subagent.list` | `{session_id}` | `{subagents: ActiveSubagent[]}` | Sub-agent panel |
| `subagent.steer` | `{session_id, subagent_id, text}` | `{steered: boolean}` | Steer sub-agent |
| `subagent.tail` | `{session_id, subagent_id}` | `{text}` | View sub-agent output |
| `subagent.interrupt` | `{session_id, subagent_id}` | `{interrupted: boolean}` | Interrupt sub-agent |
| `prompt.background` | `{session_id, task, model?}` | `{task_id}` | /background side agent |
| `prompt.btw` | `{session_id, question}` | `{task_id}` | /btw side question |
| `preview.restart` | `{session_id}` | `{task_id}` | Preview restart agent |
| `spawn_tree.save/load/list` | various | various | Sub-agent tree persistence |

### Cut (76 methods)

#### Billing / Subscription / Free Tier (14)
| Method | Notes |
|--------|-------|
| `billing.state` | Full billing state |
| `billing.step_up` | Device flow for billing scope |
| `subscription.preview` | Preview plan change |
| `subscription.upgrade` | Upgrade subscription |
| `billing.charge` | One-time charge |
| `billing.charge_status` | Poll charge status |
| `billing.auto_reload` | Configure auto-reload |
| `free_tier.status` | Free tier status |
| `free_tier.provision` | Provision free tier |
| `free_tier.ack_notice` | Acknowledge notice |
| `diagnostics.share_nous` | Share diagnostics with Nous |
| `verification.status` | Identity verification |
| `profiles.create` | Create profile |
| `onboarding.ensure_setup_profile` / `onboarding.reset_setup_profile` | Setup flow |

#### Connectors / Vault / Vault (20)
| Method | Notes |
|--------|-------|
| `connectors.list` / `connectors.connect` / `connectors.catalog` | External integrations |
| `connectors.accounts` / `connectors.accounts.remove` | Connected accounts |
| `connectors.policy.get` / `connectors.policy.set` | Connector policies |
| `connectors.tools` | Connector tools |
| `connectors.operation.status` / `connectors.operation.wake` | Long-running ops |
| `connection.respond` | Connection approval |
| `vault.list` / `vault.sources` / `vault.source.set` | Password manager |
| `vault.unlock` / `vault.lock` / `vault.add` / `vault.remove` | Vault ops |
| `vault.unlock_prompt` / `vault.save_login` / `vault.code` | Vault prompts (server→client) |

#### Voice / Wake / Pets (11)
| Method | Notes |
|--------|-------|
| `voice.toggle` / `voice.record` / `voice.tts` | Voice I/O |
| `wake.start` / `wake.stop` / `wake.pause` / `wake.resume` / `wake.status` / `wake.feed` | Wake word |
| `pet.cancel` / `pet.gallery` / `pet.info.meta` / `pet.cells` / `pet.select` | Pet sprites |

#### Bot Relay / Groups / Hosted Rooms (9)
| Method | Notes |
|--------|-------|
| `bot_relay.roster.sync` / `bot_relay.outbox.drain` / `bot_relay.deliver` / `bot_relay.reply` | Bot relay |
| `groups.capabilities` / `groups.list` / `groups.state` / `groups.peer.*` / `groups.stop` / `groups.approve` / `groups.retry` / `groups.promote` | Hosted rooms |

#### Display / Desktop GUI (12)
| Method | Notes |
|--------|-------|
| `display.status` / `display.thumbnail` / `display.start` / `display.stop` / `display.observe` / `display.install` | Desktop display |
| `display.lease.acquire` / `display.lease.release` | Display lease |
| `browser.manage` | CDP browser |
| `preview.open` / `preview.close` / `preview.act` / `preview.read` | Desktop preview |
| `layout.apply` / `pane.reveal` / `tour` / `window.read` / `terminal.read` | Desktop layout |

#### Other (10)
| Method | Notes |
|--------|-------|
| `cli.exec` | CLI command exec |
| `shell.exec` | Shell exec |
| `image.generate` | Image generation |
| `llm.oneshot` | One-shot LLM |
| `handoff.state` / `handoff.fail` | Session handoff |
| `project.facts` | Project facts |
| `gateway.capabilities` / `client.capabilities` / `ping` | Capability negotiation |
| `setup.runtime_check` | Runtime diagnostics |
| `clarify.lock` | Clarify early lock |

---

## Server → Client Events (Notifications)

### M1-core (24 events)

| Event | Payload | TUI Handler | Notes |
|-------|---------|-------------|-------|
| `gateway.ready` | `{skin, change_events, replay_epoch, heartbeat?}` | ✅ | Connection established |
| `skin.changed` | `SkinPayload` | ✅ | Live skin reload |
| `session.info` | `SessionLiveInfo` | ✅ | Session settings |
| `session.title` | `{session_id, title}` | | Auto-title |
| `session.resume_progress` | `{phase, status, message_count?, message?}` | | Resume hydration |
| `session.reclaimed` | `{session_id, stored_session_id, reason}` | | Session stolen |
| `session.control.update` | `{control: SessionControlSnapshot}` | ✅ | Goal/loop/heartbeat |
| `session.usage` | `{usage: Usage}` | | Mid-turn usage |
| `message.start` | `{}` | ✅ | Turn begins |
| `message.delta` | `{text, rendered?, verbose?}` | ✅ | Streaming text |
| `reasoning.delta` | `{text, rendered?, verbose?}` | ✅ | Streaming reasoning |
| `reasoning.available` | `{text, rendered?, verbose?}` | ✅ | Non-stream reasoning |
| `thinking.delta` | `{text, rendered?, verbose?}` | | Legacy thinking |
| `message.interim` | `{text, already_streamed}` | ✅ | Interim assistant text |
| `message.complete` | `{text, usage, status, reasoning, warning, billing, error, ...}` | ✅ | Turn complete |
| `status.update` | `{kind, text}` | ✅ | Status line |
| `tool.start` | `{tool_id, name, context?, args?, args_text?, preview?, labels?}` | ✅ | Tool call start |
| `tool.complete` | `{tool_id, name, args?, duration_s, result, summary, result_text, inline_diff?, todos?, revision?, labels?}` | ✅ | Tool call done |
| `tool.generating` | `{name}` | ✅ | Model emitting tool args |
| `tool.output_risk` | `{tool_id, name, risk, findings[], redacted}` | | Risk classification |
| `todo.updated` | `{todos[], revision}` | ✅ | Todo list |
| `notification.show` | `{text, level, kind, ttl_ms?, key?, id?}` | ✅ | Toast/status |
| `notification.clear` | `{key}` | ✅ | Dismiss toast |
| `error` | `{message}` | ✅ | Session-level error |

### Later (13 events)

| Event | Payload | Notes |
|-------|---------|-------|
| `subagent.spawn_requested` | `SubagentEventPayload` | Delegation accepted |
| `subagent.start` | `SubagentEventPayload` | Child started |
| `subagent.progress` | `SubagentEventPayload` | Batched tool progress |
| `subagent.thinking` | `SubagentEventPayload` | Child reasoning |
| `subagent.tool` | `SubagentEventPayload` | Child tool call |
| `subagent.complete` | `SubagentEventPayload` | Child finished |
| `background.complete` | `{task_id, text, question?}` | /background done |
| `btw.complete` | `{task_id, text, question?}` | /btw done |
| `preview.restart.complete` | `{task_id, text, question?}` | Preview restart done |
| `preview.restart.progress` | `{task_id, text, level?}` | Preview restart progress |
| `moa.reference` | `{label, text, index?, count?}` | Mixture of Agents |
| `moa.aggregating` | `{aggregator}` | MoA aggregator |
| `moa.progress` / `moa.phase` | progress/phase | MoA phases |

### Cut (34 events)

#### Billing / Voice / Wake / Pets (12)
| Event | Notes |
|-------|-------|
| `billing.step_up.verification` | `{verification_url, user_code}` |
| `voice.status` / `voice.transcript` / `voice.interrupted` | Voice recorder |
| `wake.detected` | `{phrase, profile?, start_new_session}` |
| `pet.changed` / `pet.generate.progress` / `pet.hatch.progress` | Pet sprites |

#### Desktop GUI / Browser (10)
| Event | Notes |
|-------|-------|
| `preview.open` / `preview.close` | Desktop preview |
| `layout.apply` / `pane.reveal` | Desktop layout |
| `message.reaction` | Emoji reaction |
| `agent.terminal.output` / `terminal.close` | Background process |
| `browser.progress` | CDP progress |
| `browser.controller.command` / `browser.controller.cancel` | Browser control |

#### Change Watcher Signals (7)
| Event | Notes |
|-------|-------|
| `cron.changed` | Refetch cron list |
| `sessions.changed` | Refetch session list |
| `projects.changed` | Refetch Projects sidebar |
| `platforms.changed` | Refetch platform status |
| `pairing.changed` | Refetch pairing |
| `bot_relay.outbox.pending` | Drain bot relay outbox |
| `tip.show` | Desktop tooltip |

#### Review / Reaction (3)
| Event | Notes |
|-------|-------|
| `review.summary` | Background review |
| `reaction` | `{kind}` - affection reaction |
| `setup.ready` | Free-tier bootstrap done |

---

## Server → Client Requests (Callbacks)

| Request | Params | Result | TUI Handles | Classification |
|---------|--------|--------|-------------|----------------|
| `clarify` | `{question, choices?, multi_select?, questions?, answers?}` | `{answer?, answers?}` | ✅ | M1-core |
| `approval` | `{request_id, command, description, choices[], allow_permanent?, allow_session?, smart_denied?, tool_name?, session_id_hint?, ...}` | `{choice, all?}` | ✅ | M1-core |
| `sudo` | `{command}` | `{value}` | | M1-core |
| `secret` | `{env_var, prompt, metadata?}` | `{value}` | | M1-core |
| `vault.unlock_prompt` | `{backend, display_name}` | `{value}` | | **cut** |
| `vault.save_login` | `{origin, site}` | `{value}` | | **cut** |
| `vault.code` | `{site?, hint?}` | `{value}` | | **cut** |
| `terminal.read` | `{start?, count?}` | `{value}` | | **cut** (desktop) |
| `preview.read` | `{start?, count?}` | `{value}` | | **cut** (desktop) |
| `window.read` | `{}` | `{value}` | | **cut** (desktop) |
| `preview.act` | `{action, ref?, selector?, text?, key?, submit?, full?, to?, amount?, max?, allow_shortcut?}` | `{value}` | | **cut** (desktop) |
| `tour` | `{action, surface?, selector?, title?, text?, side?, steps?, step_index?}` | `{value}` | | **cut** (desktop) |
| `request.cancel` | `{id, method, reason}` | — | ✅ | M1-core |

---

## Transport

### Stdio (Primary for k3code)
- **Launch:** TUI spawns `k3code gateway --stdio` as child process
- **Framing:** newline-delimited JSON-RPC 2.0 on stdin/stdout
- **Stderr:** captured as `gateway.stderr` events (log tail)
- **Handshake:** First frame from gateway is `gateway.ready` event
- **Heartbeat:** None (stdio is reliable); liveness via `gateway.ready` replay_epoch on reconnect

### WebSocket (Attach mode)
- **Env:** `HERMES_TUI_GATEWAY_URL` (ws:// or wss://)
- **Framing:** JSON-RPC 2.0 over WebSocket frames
- **Heartbeat:** `gateway.ready.heartbeat=true` enables ping/pong (30s interval, 90s deadline)
- **Sidecar:** `HERMES_TUI_SIDECAR_URL` mirrors events to dashboard
- **Reconnect:** Exponential backoff (1s base, 30s cap, no jitter)

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `HERMES_TUI_GATEWAY_URL` | Attach to existing gateway (WS URL) |
| `HERMES_TUI_SIDECAR_URL` | Mirror events to sidecar (WS URL) |
| `HERMES_PYTHON` | Python interpreter for spawned gateway |
| `HERMES_CWD` | Working directory for gateway |
| `HERMES_PYTHON_SRC_ROOT` | Hermes source root (import guard) |
| `HERMES_TUI_STARTUP_TIMEOUT_MS` | Startup timeout (default 15s) |
| `HERMES_TUI_RPC_TIMEOUT_MS` | RPC timeout (default 120s) |
| `HERMES_VOICE` | Enable voice (default 0) |

### Gateway Launch (k3code Integration)

```
k3code (CLI)
    └─ spawns k3code-tui (built TUI binary)
            └─ spawns k3code gateway --stdio (Python gateway)
                    stdin/stdout  ↔ JSON-RPC
                    stderr        → gateway.stderr events
```

**Configuration location in TUI:** `tui/src/gatewayClient.ts` → `resolvePython()`, `startSpawnedGateway()`, `startAttachedGateway()`

The TUI decides the transport:
1. If `HERMES_TUI_GATEWAY_URL` is set → WebSocket attach mode
2. Otherwise → spawn `python -m tui_gateway.entry` as child process

For k3code: `k3code` sets `HERMES_TUI_GATEWAY_URL` empty, lets TUI spawn the stdio gateway.

---

## Summary Counts

| Category | Methods | Events | Requests |
|----------|---------|--------|----------|
| **M1-core** | 33 | 24 | 4 (clarify, approval, sudo, secret) |
| **later** | 12 | 13 | 0 |
| **cut** | 76 | 34 | 9 (vault.*, terminal.*, preview.*, tour) |
| **Total** | **121** | **71** | **13** |

---

## TUI Components Referencing Cut Features

| Component | Cut Features Referenced |
|-----------|------------------------|
| `components/billingOverlay.tsx` | `billing.*`, `subscription.*`, `free_tier.*` |
| `components/subscriptionOverlay.tsx` | `subscription.preview`, `subscription.upgrade` |
| `components/petPicker.tsx` / `petSprite.tsx` / `petPolling.ts` | `pet.*` methods/events |
| `components/voiceSubmitModeRenderer.tsx` | `voice.*`, `wake.*` methods/events |
| `components/connectionSetupOverlay.tsx` | `connectors.*`, `connection.respond` |
| `components/pluginsHub.tsx` | `plugins.manage` (MCP plugins - keep) |
| `components/skillsHub.tsx` | `skills.manage` (keep) |
| `components/branding.tsx` | `billing.state` for org info |
| `app/slash/commands/subscription.ts` | Subscription slash commands |
| `app/slash/commands/wake.ts` | Wake slash commands |
| `app/slash/commands/topup.ts` | Billing topup |
| `lib/petPolling.ts` | `pet.info.meta`, `pet.cells` |
| `app/usePet.ts` | Pet state |
| `app/wakeState.ts` | Wake state |

These components should be hidden/stubbed in k3code M1. The pet/voice/wake/billing overlays can be conditionally rendered based on a `k3codeMode` flag.

---

## Next Steps for k3code M1

1. Implement the 33 M1-core methods in `core/` Python gateway
2. Emit the 24 M1-core events from the gateway
3. Handle the 4 M1-core server requests (clarify, approval, sudo, secret)
4. Hide/stub the 76 cut methods and 34 cut events
5. Wire stdio transport in `core/gateway_entry.py` matching `tui_gateway/entry.py`
6. Add `k3codeMode` flag to TUI to hide cut components
