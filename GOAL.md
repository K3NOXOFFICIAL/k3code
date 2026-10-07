# k3code — Standing Goal

Set 2026-10-07 from the owner's original request; it stays active until every milestone's exit criteria in `docs/PLAN.md` pass.

## The request (verbatim essentials)

Build a harness for coding, based on open-source projects, so there is less to write. It must:

- **Code**, and be **agentic even when left alone** (autonomous). It should **run 24/7** unattended.
- Have **loops and goals as commands**.
- Have a **multi-session mode** like the Claude Code agent view, with the states *needs input*, *working* and *completed*.
  - It is **navigable with the arrow keys**.
  - Multiple agents **appear under the input field**.
  - **↑** cycles through previous inputs.
- Have **fail fallbacks**:
  - If the API doesn't respond or the device is offline, retry until it works.
  - When offline, **pause** the work and **automatically continue** when back online.
- Have **automations**.
- Be **self-optimizing and self-improving**.
- Do **automatic task degradation**: reroute unimportant and background tasks to faster or cheaper models.
- Do **automatic fallback routing**, across models *and* across multiple providers: primary, secondary and a third fallback.
- **Think ahead**. In auto mode it always plans before executing. It decides on its own whether the task needs a huge scope or just a plan, then proceeds.
- **Fan out agents automatically**, based on project and task complexity.
- **Propose changes proactively**, for example "want me to also set up X", "this action could lead to Y", "you could improve Z".
- **Learn the user's decisions and way of thinking**, so that it:
  - proposes better;
  - handles permissions better ("you always allow X → add to permissions / do it automatically?");
  - prepares projects better.
- Have a **focus mode** that hides non-essential UI, tool calls, thinking and output. It shows only questions, the input prompt, end results and other important info.
- Provide a **TUI plus multiple windows inside the terminal**, like tuios, but with more intuitive shortcuts and edit mode.
- Be a **complete setup that is easy to install on new devices**. It includes a **setup mode** and a **guided first-run setup** covering:
  - info about the user;
  - system and environment;
  - what the install is mainly used for;
  - default theme;
  - other things.
- Provide these commands:
  `goal, loop, compact, bg, effort, model, ultracode, ultraplan, preview` (a quick rough sketch of the result), `stats, branch, clear, exit, stop, update, settings, export` (settings + session), `fork, import` (session + config), `mcp, memory, output-style, permissions, rename, resume, skills, ultraresearch, review, debug, doctor, schedule, config, update-config, add-dir, artifacts, advisor`.

## Additions made during the planning session
- Name: **k3code** (working name; to be renamed later).
- Build it as a new harness from the parts of several harnesses: Hermes, openclaw, opencode, tuios, codex, pi and atomic-agents. No single-project fork.
- Multi-window: **fork tuios** with a new zellij-style keymap.
- Private GitHub repo `K3NOXOFFICIAL/k3code`, synced with `~/src/k3code`.
- The degradation tiers are chosen in the guided setup, with no hardcoded defaults.
- **Build it mainly with OmniRoute-powered agents**, so the Claude account isn't drained. Claude handles orchestration and review only.

## Done means
All of M0–M6 in `docs/PLAN.md` meet their exit criteria, verified on the laptop.
