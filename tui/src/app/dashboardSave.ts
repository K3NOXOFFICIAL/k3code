import {
  type DashboardChange,
  dashboardSummary,
  type DashboardUi,
} from "../domain/dashboard.js";

import type { SlashRunOpts } from "./createSlashHandler.js";
import type { GatewayRpc } from "./interfaces.js";
import { patchOverlayState } from "./overlayStore.js";
import { patchUiState } from "./uiStore.js";

/**
 * /dashboard popup: Enter. The draft is painted first (the fields that waited
 * for Enter too), then each change goes out: a field with a slash command runs
 * it (it checks, saves and says what it did), any other sends one `config.set`;
 * a key the gateway refuses is named in the transcript instead of silently
 * reverting on restart.
 *
 * The first command takes the flight number, as a typed one would, so an
 * answer still on its way from an earlier command is dropped. The others run
 * `alongside` it: without that each would supersede the one before, and the
 * confirmation or the error of every command but the last would never show.
 */
export function saveDashboard(
  changes: DashboardChange[],
  patch: Partial<DashboardUi>,
  deps: {
    rpc: GatewayRpc;
    run: (command: string, opts?: SlashRunOpts) => boolean;
    sys: (text: string) => void;
  },
) {
  const { rpc, run, sys } = deps;
  let first = true;

  patchUiState(patch);
  patchOverlayState({ dashboard: null });

  const summary = dashboardSummary(changes);

  if (summary) {
    sys(summary);
  }

  for (const { command, key, value } of changes) {
    if (command) {
      run(command, first ? undefined : { alongside: true });
      first = false;

      continue;
    }

    void rpc("config.set", { key, value }).then((r) => {
      if (r === null) {
        sys(`could not save ${key}: it applies to this session only`);
      }
    });
  }
}
