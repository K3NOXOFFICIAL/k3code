import { withInkSuspended } from "@k3code/ink";

import { launchK3codeCommand } from "../../../lib/externalCli.js";
import { runExternalSetup } from "../../setupHandoff.js";
import type { SlashCommand } from "../types.js";

export const setupCommands: SlashCommand[] = [
  {
    help: "run full setup wizard (launches `k3code setup`)",
    name: "setup",
    run: (arg, ctx) =>
      void runExternalSetup({
        args: ["setup", ...arg.split(/\s+/).filter(Boolean)],
        ctx,
        done: "setup complete — starting session…",
        launcher: launchK3codeCommand,
        suspend: withInkSuspended,
      }),
  },
];
