import { withInkSuspended } from "@k3code/ink";

import { launchK3codeCommand } from "../../../lib/externalCli.js";
import { runExternalSetup } from "../../setupHandoff.js";
import type { SlashCommand } from "../types.js";

export const setupCommands: SlashCommand[] = [
  {
    help: "first-time setup, fast or full (launches `k3code onboard`); `/setup --step NAME` re-runs one wizard step",
    name: "setup",
    run: (arg, ctx) => {
      const wizardArgs = arg.split(/\s+/).filter(Boolean);

      void runExternalSetup({
        args: wizardArgs.length ? ["setup", ...wizardArgs] : ["onboard"],
        ctx,
        done: "setup complete — starting session…",
        launcher: launchK3codeCommand,
        suspend: withInkSuspended,
      });
    },
  },
];
