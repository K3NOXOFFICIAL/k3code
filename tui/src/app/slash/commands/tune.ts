import type { SlashExecResponse } from "../../../gatewayTypes.js";
import {
  openPermissionsPicker,
  openProviderPicker,
  openSettingsPanel,
  openTunePicker,
} from "../../overlayStore.js";
import type { SlashCommand, SlashRunCtx } from "../types.js";

/**
 * Bare `/tune`, bare `/model` and bare `/effort` open the SAME popup. It stays
 * shut while an approval / question / password / confirm prompt is waiting.
 */
export function openTunePopup(ctx: SlashRunCtx): void {
  if (!openTunePicker()) {
    ctx.transcript.sys(
      "answer the open prompt first, then run the command again",
    );
  }
}

/**
 * Bare `/provider` opens its own popup (the provider-block switcher, #421),
 * not the /tune popup. Typed `/provider <name> [--save]` goes to the gateway.
 */
export function openProviderPopup(ctx: SlashRunCtx): void {
  if (!openProviderPicker()) {
    ctx.transcript.sys(
      "answer the open prompt first, then run the command again",
    );
  }
}

/**
 * Bare `/permissions` opens the permissions popup (mode + effective rules),
 * not the settings panel. Typed `/permissions ...` goes to the gateway.
 */
export function openPermissionsPopup(ctx: SlashRunCtx): void {
  if (!openPermissionsPicker()) {
    ctx.transcript.sys(
      "answer the open prompt first, then run the command again",
    );
  }
}

/** `/<name> <arg>` on the gateway, which owns the /tune grammar; its message is what the user sees. */
function runOnGateway(name: string, arg: string, ctx: SlashRunCtx) {
  return ctx.gateway.gw
    .request<SlashExecResponse>("slash.exec", {
      command: arg ? `${name} ${arg}` : name,
      session_id: ctx.sid,
    })
    .then((r) => {
      if (!ctx.stale()) {
        ctx.transcript.sys(r?.output || `/${name}: no output`);
      }
    })
    .catch(ctx.guardedErr);
}

// `/ultracode [on|off|status]`, `/ultraplan` and `/ultraresearch` are deliberately NOT here: the gateway owns them,
// and the generic slash path sends them there untouched.
export const tuneCommands: SlashCommand[] = [
  {
    help: "interactive settings panel (arrows to move/change, / to search, Tab for status)",
    name: "settings",
    usage: "/settings [status]",
    run: (arg, ctx) => {
      if (arg.trim() === "status" || arg.trim() === "text") {
        return ctx.gateway.gw
          .request<SlashExecResponse>("slash.exec", {
            command: "settings",
            session_id: ctx.sid,
          })
          .then((r) => {
            if (!ctx.stale()) {
              ctx.transcript.page(
                r?.output || "/settings: no output",
                "Settings",
              );
            }
          })
          .catch(ctx.guardedErr);
      }

      if (!openSettingsPanel()) {
        ctx.transcript.sys("finish the pending prompt first");
      }
    },
  },

  {
    help: "set model, effort and ultracode together (popup, or /tune [model] [effort] [ultracode [on|off]] [--global])",
    name: "tune",
    usage: "/tune [model] [effort] [ultracode [on|off]] [--global]",
    run: (arg, ctx) =>
      arg.trim() ? runOnGateway("tune", arg.trim(), ctx) : openTunePopup(ctx),
  },

  {
    help: "set reasoning effort: popup, or /effort low|medium|high|xhigh|max|default",
    name: "effort",
    usage: "/effort [low|medium|high|xhigh|max|default]",
    run: (arg, ctx) =>
      arg.trim() ? runOnGateway("effort", arg.trim(), ctx) : openTunePopup(ctx),
  },

  {
    help: "popup: switch this session's provider or add a new one (+ Add new provider); /provider <name> [--save] switches (--save: also the default in config.yaml)",
    name: "provider",
    usage: "/provider [<name> [--save]]",
    run: (arg, ctx) =>
      arg.trim()
        ? runOnGateway("provider", arg.trim(), ctx)
        : openProviderPopup(ctx),
  },

  {
    help: "permissions (mode + rules popup, or /permissions ... passthrough to gateway)",
    name: "permissions",
    usage: "/permissions [...]",
    run: (arg, ctx) => {
      if (arg.trim()) {
        return runOnGateway("permissions", arg.trim(), ctx);
      }

      if (!openPermissionsPicker()) {
        ctx.transcript.sys("finish the pending prompt first");
      }
    },
  },
];
