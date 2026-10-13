import {
  DASHBOARD_TABS,
  parseDashboardTab,
} from "../../../domain/dashboard.js";
import { openDashboard } from "../../overlayStore.js";
import type { SlashCommand } from "../types.js";

const USAGE = `/dashboard [${DASHBOARD_TABS.map((t) => t.id).join("|")}]`;

/**
 * `/dashboard` and its alias `/display` open one tabbed popup (Look, Details,
 * Input). An argument opens it on that tab. It stays shut while an approval /
 * question / password / confirm prompt is waiting.
 */
export const dashboardCommands: SlashCommand[] = [
  {
    aliases: ["display"],
    help: "display, details and input settings in one popup (statusbar, indicator, details, mouse, busy mode, focus)",
    name: "dashboard",
    run: (arg, ctx) => {
      const word = arg.trim();
      const tab = word ? parseDashboardTab(word) : "look";

      if (!tab) {
        return ctx.transcript.sys(`usage: ${USAGE}`);
      }

      if (!openDashboard(tab)) {
        ctx.transcript.sys(
          "answer the open prompt first, then run the command again",
        );
      }
    },
    usage: USAGE,
  },
];
