# k3code TUI Integration Guide

This document explains how to launch the built `@k3code/tui` against a custom gateway command (`k3code gateway --stdio`).

## Overview

```
k3code (CLI entry)
    │
    ├─► Spawns: k3code-tui (the built Ink/React TUI binary)
    │              │
    │              └─► Spawns: k3code gateway --stdio (Python gateway)
    │                           │
    │                           ├─ stdin/stdout → JSON-RPC 2.0 (newline-delimited)
    │                           └─ stderr       → gateway.stderr events
    │
    └─► (Future) Attaches to: k3code-host (WebSocket on 127.0.0.1)
```

## Where the Gateway Command Is Configured

**File:** `tui/src/gatewayClient.ts`

### Key Functions

1. **`resolvePython()`** (line ~180) — Determines the Python interpreter:

   ```typescript
   const configured = process.env.K3CODE_PYTHON?.trim();
   if (configured) return configured;
   return process.platform === "win32" ? "python" : "python3";
   ```

2. **`startSpawnedGateway(root: string)`** (line ~420) — Spawns the gateway child process:

   ```typescript
   this.proc = spawn(python, ["-m", "tui_gateway.entry"], {
     cwd,
     env,
     stdio: ["pipe", "pipe", "pipe"],
   });
   ```

3. **`startAttachedGateway(attachUrl: string)`** (line ~490) — Attaches to existing gateway via WebSocket.

4. **`resolveGatewayAttachUrl()`** (line ~160) — Checks for attach mode:
   ```typescript
   const raw = process.env.K3CODE_TUI_GATEWAY_URL?.trim();
   return raw ? raw : null;
   ```

### Transport Decision Logic (in `start()`)

```typescript
const attachUrl = resolveGatewayAttachUrl();

if (attachUrl) {
  this.startAttachedGateway(attachUrl); // WebSocket attach mode
  return;
}

this.startSpawnedGateway(root); // Stdio spawn mode (DEFAULT)
```

---

## For k3code: Stdio Mode (Default)

The TUI **defaults to spawning the gateway** when `K3CODE_TUI_GATEWAY_URL` is not set.

### What k3code Needs to Do

1. **Build the TUI:**

   ```bash
   cd tui && npm run build
   # Output: tui/dist/entry.js (entry point)
   # Binary: k3code-tui (from package.json "bin" field)
   ```

2. **Launch from k3code CLI:**

   ```javascript
   // In k3code's main entry point
   const { spawn } = require("node:child_process");

   // 1. Spawn the TUI
   const tui = spawn("k3code-tui", [], {
     stdio: ["inherit", "inherit", "inherit", "ipc"], // or pipe for control
     env: {
       ...process.env,
       K3CODE_PYTHON: "/path/to/k3code-gateway-python", // Optional: explicit python
       K3CODE_CWD: process.cwd(), // Working dir for gateway
       K3CODE_PYTHON_SRC_ROOT: "/path/to/k3code/core", // Python source root
       // DO NOT SET: K3CODE_TUI_GATEWAY_URL (leave unset for stdio mode)
     },
   });

   // 2. The TUI will spawn: python -m tui_gateway.entry
   //    You need to ensure the gateway module is importable from K3CODE_PYTHON_SRC_ROOT
   ```

3. **Gateway Module Structure Expected by TUI:**
   ```
   K3CODE_PYTHON_SRC_ROOT/
   └── tui_gateway/
       ├── entry.py          # <-- spawned as `python -m tui_gateway.entry`
       ├── server.py         # JSON-RPC handlers
       ├── transport.py      # Stdout/stdin transport
       ├── event_replay.py   # Replay epoch
       └── contracts/        # Pydantic models (shared with TUI via generated TS)
   ```

---

## For k3code: Attach Mode (Future)

When `k3code-host` is running, the CLI can attach the TUI to it:

```bash
# Terminal 1: Start the host (persistent gateway)
k3code host start  # Listens on ws://127.0.0.1:PORT

# Terminal 2: Attach TUI
K3CODE_TUI_GATEWAY_URL=ws://127.0.0.1:PORT k3code-tui
```

The TUI will connect via WebSocket instead of spawning a child.

---

## Required Gateway Contract (M1-core)

The TUI expects the gateway to implement these **33 methods** and emit **24 events**:

### Methods (Client → Server)

- `session.create`, `session.list`, `session.active_list`, `session.resume`, `session.activate`, `session.delete`, `session.interrupt`, `session.steer`, `session.control.read`
- `prompt.submit`, `clipboard.paste`, `image.attach`, `image.detach`, `input.detect_drop`
- `command.dispatch`, `slash.exec`
- `model.options`, `model.save_key`, `model.disconnect`
- `config.get` (keys: "full", "mtime"), `config.set`
- `system.battery`, `setup.status`

### Events (Server → Client)

- `gateway.ready`, `skin.changed`, `session.info`, `session.control.update`
- `message.start`, `message.delta`, `reasoning.delta`, `reasoning.available`, `thinking.delta`
- `message.interim`, `message.complete`
- `status.update`
- `tool.start`, `tool.complete`, `tool.generating`, `todo.updated`
- `notification.show`, `notification.clear`
- `error`

### Server Requests (Server → Client Callbacks)

- `clarify`, `approval`, `sudo`, `secret`
- `request.cancel` (withdrawal notification)

---

## Environment Variables Reference

| Variable                        | Required            | Default                           | Description                                                                                                                         |
| ------------------------------- | ------------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `K3CODE_TUI_GATEWAY_URL`        | No                  | (unset)                           | WebSocket URL to attach to existing gateway. If unset, TUI spawns gateway.                                                          |
| `K3CODE_GATEWAY_CMD`            | No                  | (unset)                           | Full command for the spawned gateway (shell-style quoting), e.g. `python -m k3code.cli gateway --stdio`. Overrides `K3CODE_PYTHON`. |
| `K3CODE_TUI_SIDECAR_URL`        | No                  | (unset)                           | WebSocket URL to mirror events to (dashboard).                                                                                      |
| `K3CODE_PYTHON`                 | No                  | `python3` / `python`              | Python interpreter for spawned gateway.                                                                                             |
| `K3CODE_CWD`                    | No                  | `process.cwd()`                   | Working directory for gateway process.                                                                                              |
| `K3CODE_PYTHON_SRC_ROOT`        | **Yes** (for spawn) | `import.meta.dirname + '/../../'` | Root where `tui_gateway` package is importable.                                                                                     |
| `K3CODE_TUI_STARTUP_TIMEOUT_MS` | No                  | `15000`                           | Max wait for `gateway.ready`.                                                                                                       |
| `K3CODE_TUI_RPC_TIMEOUT_MS`     | No                  | `120000`                          | RPC request timeout.                                                                                                                |
| `HERMES_VOICE`                  | No                  | `0`                               | Set to `1` to enable voice features.                                                                                                |

---

## Minimal Gateway Implementation Checklist

For `k3code gateway --stdio` to work with the TUI:

- [ ] Entry point: `python -m k3code_gateway.entry` (or similar)
- [ ] Stdout/stdin: newline-delimited JSON-RPC 2.0
- [ ] First frame: `{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"skin":...,"change_events":true,"replay_epoch":"..."}}}`
- [ ] Handle all 33 M1-core methods
- [ ] Emit all 24 M1-core events
- [ ] Handle 4 server requests (clarify, approval, sudo, secret)
- [ ] Stderr lines → `gateway.stderr` events (optional but recommended)
- [ ] Graceful shutdown on stdin EOF / SIGTERM

---

## Testing the Integration

```bash
# 1. Build TUI
cd /path/to/k3code/tui && npm run build

# 2. Test with a mock gateway (echo server)
cat > /tmp/mock_gateway.py << 'PYEOF'
import sys, json
print(json.dumps({"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready","payload":{"skin":{"name":"test","colors":{},"light_colors":{},"dark_colors":{},"branding":{},"banner_logo":"","banner_hero":"","tool_prefix":"","help_header":""},"change_events":true,"replay_epoch":"test"}}}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req.get("method") == "session.create":
        print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":{"session_id":"test-123","info":{"model":"test","tools":[]}}}), flush=True)
PYEOF

# 3. Run TUI against mock
K3CODE_PYTHON=python3 K3CODE_PYTHON_SRC_ROOT=/tmp node dist/entry.js
```

---

## File Locations Summary

| File                                       | Purpose                                    |
| ------------------------------------------ | ------------------------------------------ |
| `tui/src/gatewayClient.ts`                 | Transport logic, spawn/attach, RPC channel |
| `tui/src/app/createGatewayEventHandler.ts` | Event handlers (switch on `ev.type`)       |
| `tui/src/app/useMainApp.ts`                | Main app, subscribes to gateway events     |
| `tui/shared/gateway-contract.generated.ts` | TypeScript types (generated from Python)   |
| `tui/shared/gateway-events.ts`             | Event type definitions                     |
| `tui/scripts/build/tui.mjs`                | Build script (esbuild)                     |
| `tui/package.json`                         | `"bin": {"k3code-tui": "./dist/entry.js"}` |
