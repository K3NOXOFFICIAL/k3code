import type { SlashExecResponse } from "../../../gatewayTypes.js";
import { openTunePicker } from "../../overlayStore.js";
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

/** `/<name> <arg>` on the gateway, which owns the /tune grammar; its message is what the user sees. */
function runOnGateway(name: string, arg: string, ctx: SlashRunCtx) {
  return ctx.gateway.gw
    .request<SlashExecResponse>("slash.exec", {
      command: `${name} ${arg}`,
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
];
