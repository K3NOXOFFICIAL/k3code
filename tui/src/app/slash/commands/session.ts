import {
  sessionScopedModelArg,
  TUI_SESSION_MODEL_FLAG,
} from "../../../domain/slash.js";
import { parsePetCommand, PET_MIN_COLS } from "../../../lib/terminalPet.js";
import type {
  ConfigGetValueResponse,
  ConfigSetResponse,
  SessionUsageResponse,
  SlashExecResponse,
} from "../../../gatewayTypes.js";
import type { PanelSection } from "../../../types.js";
import { applyConfiguredTuiTheme } from "../../createGatewayEventHandler.js";
import {
  DEFAULT_INDICATOR_STYLE,
  INDICATOR_STYLES,
  type IndicatorStyle,
} from "../../interfaces.js";
import { patchOverlayState } from "../../overlayStore.js";
import {
  $petEnabled,
  $petName,
  $petParty,
  petConfigValue,
  setPetEnabled,
  setPetName,
  setPetParty,
} from "../../petStore.js";
import { patchUiState } from "../../uiStore.js";
import type { SlashCommand } from "../types.js";

const TUI_SESSION_MODEL_RE = new RegExp(
  `(?:^|\\s)${TUI_SESSION_MODEL_FLAG}(?:\\s|$)`,
);
const REASONING_SESSION_FLAGS = new Set(["--session"]);
const REASONING_GLOBAL_FLAGS = new Set(["--global"]);

const modelValueForConfigSet = (arg: string) => {
  const trimmed = arg.trim();

  if (!trimmed) {
    return trimmed;
  }

  if (TUI_SESSION_MODEL_RE.test(trimmed)) {
    return sessionScopedModelArg(trimmed);
  }

  return trimmed;
};

const reasoningConfigPayload = (arg: string, sid: string) => {
  const parts = arg.trim().split(/\s+/).filter(Boolean);
  let scope = "";
  const valueParts: string[] = [];

  for (const part of parts) {
    const flag = part.toLowerCase();

    if (REASONING_GLOBAL_FLAGS.has(flag)) {
      scope = "global";

      continue;
    }

    if (REASONING_SESSION_FLAGS.has(flag)) {
      // Session scope is the default; accept the flag for parity with /model.
      if (!scope) {
        scope = "session";
      }

      continue;
    }

    valueParts.push(part);
  }

  const value = valueParts.join(" ");

  return {
    key: "reasoning",
    session_id: sid,
    value,
    ...(scope ? { scope } : {}),
  };
};

export const sessionCommands: SlashCommand[] = [
  // k3: no local /bg. The gateway's /bg handles `/bg <prompt>`, `/bg --pane <prompt>` and a bare `/bg`
  // (send the running turn to the background); the old local handler only knew `/bg <prompt>`.

  {
    help: "change or show model",
    name: "model",
    run: (arg, ctx, cmd) => {
      // k3: `/model chain [add|remove|move …]` is the gateway's fallback-chain view, not a model switch.
      if (/^chain(\s|$)/.test(arg.trim())) {
        return ctx.gateway.gw
          .request<SlashExecResponse>("slash.exec", {
            command: cmd.slice(1),
            session_id: ctx.sid,
          })
          .then((r) => {
            if (ctx.stale()) {
              return;
            }

            ctx.transcript.page(
              r?.output || "/model chain: no output",
              "Fallback chain",
            );
          })
          .catch(ctx.guardedErr);
      }

      // No busy guard here (unlike session switching). A model change is a
      // session-scoped config.set: idle it switches immediately; mid-turn the
      // gateway QUEUES it and applies it at the next turn start (returning
      // deferred:true) instead of rejecting. Either way the pick sticks without
      // interrupting the stream or waiting on the swap.
      if (!arg.trim()) {
        return patchOverlayState({ modelPicker: true });
      }

      if (arg.trim() === "--refresh") {
        return patchOverlayState({ modelPicker: { refresh: true } });
      }

      const switchModel = (confirmExpensiveModel = false) =>
        ctx.gateway
          .rpc<ConfigSetResponse>("config.set", {
            confirm_expensive_model: confirmExpensiveModel,
            key: "model",
            session_id: ctx.sid,
            value: modelValueForConfigSet(arg),
          })
          .then(
            ctx.guarded<ConfigSetResponse>((r) => {
              if (r.confirm_required) {
                patchOverlayState({
                  confirm: {
                    cancelLabel: "Cancel",
                    confirmLabel: "Switch anyway",
                    danger: true,
                    detail:
                      r.confirm_message ||
                      r.warning ||
                      "This model has unusually high known pricing.",
                    onConfirm: () => switchModel(true),
                    title: "Expensive model selection",
                  },
                });

                return;
              }

              if (!r.value) {
                return ctx.transcript.sys(
                  "error: invalid response: model switch",
                );
              }

              ctx.transcript.sys(
                r.deferred
                  ? `model → ${r.value} (applies next turn)`
                  : `model → ${r.value}`,
              );
              ctx.local.maybeWarn(r);

              patchUiState((state) => ({
                ...state,
                info: state.info
                  ? { ...state.info, model: r.value! }
                  : { model: r.value!, skills: {}, tools: {} },
              }));
            }),
          );

      switchModel();
    },
  },

  {
    aliases: ["switch", "session", "resume"],
    help: "browse, switch, or resume sessions",
    name: "sessions",
    run: (arg, ctx) => {
      const trimmed = arg.trim();

      // A new *live* session keeps the current one running in the background
      // (it doesn't close it), so fanning out while busy is allowed — that's
      // the whole point of multiple live sessions.
      if (trimmed.toLowerCase() === "new") {
        return ctx.session.newLiveSession();
      }

      // `/resume <id|title>` (and `/sessions <id>`) load a cold session and
      // CLOSE the current one, so guard it while a turn is in-flight to avoid
      // corrupting streaming/busy state. Bare opens the overlay to browse.
      if (trimmed) {
        if (ctx.session.guardBusySessionSwitch("switch sessions")) {
          return;
        }

        return ctx.session.resumeById(trimmed);
      }

      patchOverlayState({ sessions: true });
    },
  },

  {
    help: "attach an image",
    name: "image",
    run: (arg, ctx) => ctx.composer.attachImagePath(arg),
  },

  {
    help: "pin light/dark mode or trust auto-detection (usage: /theme [auto|light|dark])",
    name: "theme",
    usage: "/theme [auto|light|dark]",
    run: (arg, ctx) => {
      const value = arg.trim().toLowerCase();

      if (!value) {
        return ctx.gateway
          .rpc<ConfigGetValueResponse>("config.get", { key: "theme" })
          .then(
            ctx.guarded<ConfigGetValueResponse>((r) =>
              ctx.transcript.sys(`theme: ${r.value || "auto"}`),
            ),
          );
      }

      if (!["auto", "light", "dark"].includes(value)) {
        return ctx.transcript.sys("usage: /theme [auto|light|dark]");
      }

      // Apply only after the write is confirmed (mirrors /indicator): a
      // failed config.set must not leave the session showing a theme that
      // reverts on restart. A few ms later than an optimistic flip, but the
      // env/theme state and config.yaml never disagree.
      ctx.gateway
        .rpc<ConfigSetResponse>("config.set", { key: "theme", value })
        .then(
          ctx.guarded<ConfigSetResponse>((r) => {
            if (r.value === undefined) {
              return;
            }

            applyConfiguredTuiTheme(value);
            ctx.transcript.sys(`theme → ${value}`);
          }),
        )
        .catch(ctx.guardedErr);
    },
  },

  {
    help: "terminal pet: /pet [on|off|toggle|status|random|party|solo|<name>]",
    name: "pet",
    usage: "/pet [on|off|toggle|status|random|party|solo|<name>]",
    run: (arg, ctx) => {
      const word = arg.trim().toLowerCase();
      const result = parsePetCommand(arg, {
        enabled: $petEnabled.get(),
        name: $petName.get(),
        party: $petParty.get(),
      });

      if (result.enabled !== undefined) {
        setPetEnabled(result.enabled);
      }

      if (result.name !== undefined) {
        setPetName(result.name);
      }

      if (result.party !== undefined) {
        setPetParty(result.party);
      }

      // Party is a session choice and is not saved; the saved value keeps the pinned pet.
      if (
        (result.enabled !== undefined || result.name !== undefined) &&
        result.party === undefined
      ) {
        // Pin the species only when the user named one; `random` re-rolls on every launch.
        const value = petConfigValue(
          $petEnabled.get(),
          word === result.name ? result.name : null,
        );

        ctx.gateway
          .rpc<ConfigSetResponse>("config.set", { key: "pet", value })
          .catch(() => {});
      }

      const narrow =
        $petEnabled.get() && (process.stdout.columns ?? 0) < PET_MIN_COLS;
      const hint = $petParty.get()
        ? `two or three pets at ${PET_MIN_COLS}+ columns`
        : `shown at ${PET_MIN_COLS}+ columns`;

      ctx.transcript.sys(
        narrow ? `${result.message} (${hint})` : result.message,
      );
    },
  },

  {
    help: "pick the busy indicator: kaomoji (default), emoji, unicode (braille), or ascii",
    name: "indicator",
    usage: `/indicator [${INDICATOR_STYLES.join("|")}]`,
    run: (arg, ctx) => {
      const value = arg.trim().toLowerCase();

      if (!value) {
        return ctx.gateway
          .rpc<ConfigGetValueResponse>("config.get", { key: "indicator" })
          .then(
            ctx.guarded<ConfigGetValueResponse>((r) =>
              ctx.transcript.sys(
                `indicator: ${r.value || DEFAULT_INDICATOR_STYLE}`,
              ),
            ),
          );
      }

      if (!(INDICATOR_STYLES as readonly string[]).includes(value)) {
        return ctx.transcript.sys(
          `usage: /indicator [${INDICATOR_STYLES.join("|")}]`,
        );
      }

      ctx.gateway
        .rpc<ConfigSetResponse>("config.set", { key: "indicator", value })
        .then(
          ctx.guarded<ConfigSetResponse>((r) => {
            if (!r.value) {
              return;
            }

            // Hot-swap the running TUI immediately so the next render
            // uses the new style without waiting for the 5s mtime poll
            // to re-apply config.full.
            patchUiState({ indicatorStyle: value as IndicatorStyle });
            ctx.transcript.sys(`indicator → ${r.value}`);
          }),
        );
    },
  },

  {
    help: "toggle yolo mode (per-session approvals)",
    name: "yolo",
    run: (_arg, ctx) => {
      ctx.gateway
        .rpc<ConfigSetResponse>("config.set", {
          key: "yolo",
          session_id: ctx.sid,
        })
        .then(
          ctx.guarded<ConfigSetResponse>((r) =>
            ctx.transcript.sys(`yolo ${r.value === "1" ? "on" : "off"}`),
          ),
        );
    },
  },

  {
    help: "show or hide reasoning, or set effort [show|hide|low|medium|high|xhigh|max|default]",
    name: "reasoning",
    run: (arg, ctx) => {
      if (!arg) {
        return ctx.gateway
          .rpc<ConfigGetValueResponse>("config.get", {
            key: "reasoning",
            session_id: ctx.sid,
          })
          .then(
            ctx.guarded<ConfigGetValueResponse>(
              (r) =>
                r.value &&
                ctx.transcript.sys(
                  `reasoning: ${r.value} · display ${r.display || "hide"}`,
                ),
            ),
          );
      }

      ctx.gateway
        .rpc<ConfigSetResponse>(
          "config.set",
          reasoningConfigPayload(arg, ctx.sid ?? ""),
        )
        .then(
          ctx.guarded<ConfigSetResponse>((r) => {
            if (!r.value) {
              return;
            }

            if (r.value === "hide") {
              patchUiState((state) => ({
                ...state,
                sections: { ...state.sections, thinking: "hidden" },
                showReasoning: false,
              }));
            } else if (r.value === "show") {
              patchUiState((state) => ({
                ...state,
                sections: { ...state.sections, thinking: "expanded" },
                showReasoning: true,
              }));
            }

            ctx.transcript.sys(`reasoning: ${r.value}`);
          }),
        );
    },
  },

  {
    help: "control busy enter mode [queue|steer|interrupt|status]",
    name: "busy",
    run: (arg, ctx) => {
      const mode = arg.trim().toLowerCase();
      const valid = new Set(["", "status", "queue", "steer", "interrupt"]);

      if (!valid.has(mode)) {
        return ctx.transcript.sys(
          "usage: /busy [queue|steer|interrupt|status]",
        );
      }

      if (!mode || mode === "status") {
        return ctx.gateway
          .rpc<ConfigGetValueResponse>("config.get", { key: "busy" })
          .then(
            ctx.guarded<ConfigGetValueResponse>((r) => {
              const current = r.value || "interrupt";
              ctx.transcript.sys(`busy input mode: ${current}`);
            }),
          )
          .catch(ctx.guardedErr);
      }

      ctx.gateway
        .rpc<ConfigSetResponse>("config.set", { key: "busy", value: mode })
        .then(
          ctx.guarded<ConfigSetResponse>((r) => {
            const next = r.value || mode;
            ctx.transcript.sys(`busy input mode: ${next}`);
          }),
        )
        .catch(ctx.guardedErr);
    },
  },

  {
    help: "session usage",
    name: "usage",
    run: (_arg, ctx) => {
      ctx.gateway
        .rpc<SessionUsageResponse>("session.usage", { session_id: ctx.sid })
        .then((r) => {
          if (ctx.stale()) {
            return;
          }

          const sys = ctx.transcript.sys;

          if (r) {
            patchUiState({
              usage: {
                calls: r.calls ?? 0,
                input: r.input ?? 0,
                output: r.output ?? 0,
                total: r.total ?? 0,
              },
            });
          }

          // k3code M1 cut: the billing/subscription "balance" panel (plan name,
          // credits, low-balance nudges toward /subscription and /topup) is
          // removed — this command now shows only token/call usage.
          if (!r?.calls) {
            sys("no API calls yet");

            return;
          }

          const f = (v: number | undefined) => (v ?? 0).toLocaleString();

          const rows: [string, string][] = [
            ["Model", r.model ?? ""],
            ["Input tokens", f(r.input)],
            ["Output tokens", f(r.output)],
            ["Total tokens", f(r.total)],
            ["API calls", f(r.calls)],
          ];

          const sections: PanelSection[] = [{ rows }];

          if (r.context_max) {
            const mark = r.context_estimated ? "~" : "";
            sections.push({
              text: `Context: ${mark}${f(r.context_used)} / ${f(r.context_max)} (${mark}${r.context_percent}%)`,
            });
          }

          if (r.compressions) {
            sections.push({ text: `Compressions: ${r.compressions}` });
          }

          ctx.transcript.panel("Usage", sections);
        });
    },
  },
];
