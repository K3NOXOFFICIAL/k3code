import type { PanelSection } from "../types.js";

export const SETUP_REQUIRED_TITLE = "Setup Required";

export const buildSetupRequiredSections = (): PanelSection[] => [
  {
    text: "k3code needs a model provider before the TUI can start a session.",
  },
  {
    rows: [
      [
        "/setup",
        "run `k3code onboard` in-place: fast setup asks only for an endpoint and key",
      ],
      ["/model", "pick a model (needs a session — add a provider first)"],
      ["Ctrl+C", "exit and run `k3code onboard` manually"],
    ],
    title: "Actions",
  },
];
