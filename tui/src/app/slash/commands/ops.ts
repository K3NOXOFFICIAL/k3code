import type {
  BrowserManageResponse,
  CommandsCatalogResponse,
  DelegationPauseResponse,
  ReloadEnvResponse,
  ReloadMcpResponse,
} from "../../../gatewayTypes.js";
import {
  applyDelegationStatus,
  getDelegationState,
} from "../../delegationStore.js";
import { patchOverlayState } from "../../overlayStore.js";
import {
  getSpawnHistory,
  setDiffPair,
  type SpawnSnapshot,
} from "../../spawnHistoryStore.js";
import type { SlashCommand } from "../types.js";

interface SkillsReloadResponse {
  output?: string;
}

export const opsCommands: SlashCommand[] = [
  // k3: no local /stop. The gateway's /stop interrupts the running turn; the old handler called the Hermes-only
  // `process.stop` RPC, which the k3code gateway does not have (so it always errored).

  {
    aliases: ["reload_mcp"],
    help: "reload MCP servers in the live session (warns about prompt cache invalidation)",
    name: "reload-mcp",
    run: (arg, ctx) => {
      // Parse arg: `now` / `always` skip the confirmation gate.
      // `always` additionally persists approvals.mcp_reload_confirm=false.
      const a = (arg || "").trim().toLowerCase();

      const params: {
        session_id: string | null;
        confirm?: boolean;
        always?: boolean;
      } = {
        session_id: ctx.sid,
      };

      if (a === "now" || a === "approve" || a === "once" || a === "yes") {
        params.confirm = true;
      } else if (a === "always") {
        params.confirm = true;
        params.always = true;
      }

      ctx.gateway
        .rpc<ReloadMcpResponse>("reload.mcp", params)
        .then(
          ctx.guarded<ReloadMcpResponse>((r) => {
            if (r.status === "confirm_required") {
              ctx.transcript.sys(
                r.message || "/reload-mcp requires confirmation",
              );

              return;
            }

            if (r.status === "reloaded") {
              ctx.transcript.sys(
                params.always
                  ? "MCP servers reloaded · future /reload-mcp will run without confirmation"
                  : "MCP servers reloaded",
              );

              return;
            }

            ctx.transcript.sys("reload complete");
          }),
        )
        .catch(ctx.guardedErr);
    },
  },

  {
    help: "re-read config.yaml and ~/.config/k3code/env into the running gateway",
    name: "reload",
    run: (_arg, ctx) => {
      ctx.gateway
        .rpc<ReloadEnvResponse>("reload.env", {})
        .then(
          ctx.guarded<ReloadEnvResponse>((r) => {
            const n = Number(r.updated ?? 0);
            const noun = n === 1 ? "var" : "vars";

            ctx.transcript.sys(`reloaded config and env file (${n} ${noun})`);
          }),
        )
        .catch(ctx.guardedErr);
    },
  },

  {
    help: "manage browser CDP connection [connect|disconnect|status]",
    name: "browser",
    run: (arg, ctx) => {
      const [rawAction = "status", ...rest] = arg
        .trim()
        .split(/\s+/)
        .filter(Boolean);
      const action = rawAction.toLowerCase();

      if (!["connect", "disconnect", "status"].includes(action)) {
        return ctx.transcript.sys(
          "usage: /browser [connect|disconnect|status] [url] · persistent: set browser.cdp_url in config.yaml",
        );
      }

      const sid = ctx.sid ?? null;
      const url =
        action === "connect"
          ? rest.join(" ").trim() || "http://127.0.0.1:9222"
          : undefined;

      if (url) {
        ctx.transcript.sys(
          `checking Chromium-family browser remote debugging at ${url}...`,
        );
      }

      ctx.gateway
        .rpc<BrowserManageResponse>("browser.manage", {
          action,
          session_id: sid,
          ...(url && { url }),
        })
        .then(
          ctx.guarded<BrowserManageResponse>((r) => {
            // Without a session we can't subscribe to streamed
            // browser.progress events, so flush the bundled list.
            if (!sid) {
              r.messages?.forEach((message) => ctx.transcript.sys(message));
            }

            if (action === "status") {
              return ctx.transcript.sys(
                r.connected
                  ? `browser connected: ${r.url || "(url unavailable)"}`
                  : "browser not connected (try /browser connect <url> or set browser.cdp_url in config.yaml)",
              );
            }

            if (action === "disconnect") {
              return ctx.transcript.sys("browser disconnected");
            }

            if (r.connected) {
              ctx.transcript.sys(
                "Browser connected to live Chromium-family browser via CDP",
              );
              ctx.transcript.sys(`Endpoint: ${r.url || "(url unavailable)"}`);
              ctx.transcript.sys(
                "next browser tool call will use this CDP endpoint",
              );
            }
          }),
        )
        .catch(ctx.guardedErr);
    },
  },

  {
    aliases: ["tasks"],
    help: "open the spawn-tree dashboard (live audit + kill/pause controls)",
    name: "agents",
    run: (arg, ctx) => {
      const sub = arg.trim().toLowerCase();

      // Stay compatible with the gateway `/agents [pause|resume|status]` CLI —
      // explicit subcommands skip the overlay and act directly so scripts and
      // multi-step flows can drive it without entering interactive mode.
      if (sub === "pause" || sub === "resume" || sub === "unpause") {
        const paused = sub === "pause";
        ctx.gateway.gw
          .request<DelegationPauseResponse>("delegation.pause", { paused })
          .then((r) => {
            applyDelegationStatus({ paused: r?.paused });
            ctx.transcript.sys(
              `delegation · ${r?.paused ? "paused" : "resumed"}`,
            );
          })
          .catch(ctx.guardedErr);

        return;
      }

      if (sub === "status") {
        const d = getDelegationState();
        ctx.transcript.sys(
          `delegation · ${d.paused ? "paused" : "active"} · caps d${d.maxSpawnDepth ?? "?"}/${d.maxConcurrentChildren ?? "?"}`,
        );

        return;
      }

      patchOverlayState({ agents: true, agentsInitialHistoryIndex: 0 });
    },
  },

  {
    help: "replay a completed spawn tree · `/replay [N|last]`",
    name: "replay",
    run: (arg, ctx) => {
      const history = getSpawnHistory();
      const raw = arg.trim();
      const lower = raw.toLowerCase();

      // ── In-memory nav (same-session) ─────────────────────────────
      if (!history.length) {
        return ctx.transcript.sys("no completed spawn trees this session");
      }

      let index = 1;

      if (raw && lower !== "last") {
        const parsed = parseInt(raw, 10);

        if (Number.isNaN(parsed) || parsed < 1 || parsed > history.length) {
          return ctx.transcript.sys(
            `replay: index out of range 1..${history.length}`,
          );
        }

        index = parsed;
      }

      patchOverlayState({ agents: true, agentsInitialHistoryIndex: index });
    },
  },

  {
    help: "diff two completed spawn trees · `/replay-diff <baseline> <candidate>` (history indexes, 1 = last)",
    name: "replay-diff",
    run: (arg, ctx) => {
      const parts = arg.trim().split(/\s+/).filter(Boolean);

      if (parts.length !== 2) {
        return ctx.transcript.sys(
          "usage: /replay-diff <a> <b>  (e.g. /replay-diff 1 2 for last two)",
        );
      }

      const [a, b] = parts;
      const history = getSpawnHistory();

      const resolve = (token: string): null | SpawnSnapshot => {
        const n = parseInt(token!, 10);

        if (Number.isFinite(n) && n >= 1 && n <= history.length) {
          return history[n - 1] ?? null;
        }

        return null;
      };

      const baseline = resolve(a!);
      const candidate = resolve(b!);

      if (!baseline || !candidate) {
        return ctx.transcript.sys(
          `replay-diff: could not resolve indices · history has ${history.length} entries`,
        );
      }

      setDiffPair({ baseline, candidate });
      patchOverlayState({ agents: true, agentsInitialHistoryIndex: 0 });
    },
  },

  {
    aliases: ["reload_skills"],
    help: "re-scan installed skills in the live TUI gateway",
    name: "reload-skills",
    run: (_arg, ctx) => {
      // Bound to the session so the rescan and the refreshed catalog see its
      // repo's project-local skills, not the launch environment's.
      const params = ctx.sid ? { session_id: ctx.sid } : {};

      ctx.gateway
        .rpc<SkillsReloadResponse>("skills.reload", params)
        .then(
          ctx.guarded<SkillsReloadResponse>((r) => {
            ctx.transcript.page(r.output || "skills reloaded", "Reload Skills");
            // optional (the gateway may not offer a catalog): straight to the client, so a miss stays silent
            ctx.gateway.gw
              .request<CommandsCatalogResponse>("commands.catalog", params)
              .then(
                ctx.guarded<CommandsCatalogResponse>((catalog) => {
                  if (!catalog?.pairs) {
                    return;
                  }

                  ctx.local.setCatalog({
                    canon: (catalog.canon ?? {}) as Record<string, string>,
                    categories: catalog.categories ?? [],
                    pairs: catalog.pairs as [string, string][],
                    skillCount: (catalog.skill_count ?? 0) as number,
                    sub: (catalog.sub ?? {}) as Record<string, string[]>,
                  });
                }),
              )
              .catch(() => {});
          }),
        )
        .catch(ctx.guardedErr);
    },
  },
];
