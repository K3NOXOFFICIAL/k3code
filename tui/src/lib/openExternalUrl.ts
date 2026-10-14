import { spawn, type SpawnOptions } from "node:child_process";
import { platform } from "node:os";

/**
 * Opens an external URL in the user's default browser/handler.
 *
 * Wired into the Ink instance via `onHyperlinkClick` in entry.tsx, so any
 * mouse click on a `<Link>` cell (or a row containing a plain-text URL the
 * renderer detected) goes here. Mouse tracking inside the TUI prevents
 * Terminal.app's native Cmd+click from firing — the click is captured
 * before the terminal application sees it — so we have to handle the open
 * ourselves.
 *
 * Safety:
 * - http(s) only. Anything else (`file:`, `data:`, `javascript:`, etc.) is
 *   rejected — a hostile model could otherwise emit `<Link url="file:///">`
 *   and trick a click into running an arbitrary local handler.
 * - Hostname is parsed via `URL`; only well-formed URLs are forwarded.
 * - Spawned via `child_process.spawn` with arg array (no shell), so a URL
 *   containing shell metacharacters (`;`, `&`, backticks) cannot be
 *   interpreted as a command.
 *
 * Under WSL `process.platform` is "linux" but WSL images usually ship no
 * xdg-open or browser, so `openCommands` tries `wslview`, then
 * `explorer.exe`, before `xdg-open`; a missing binary (async ENOENT) moves on
 * to the next opener. When every opener fails, the failure is reported via
 * `onOpenFailure` listeners (the app shows it in the transcript).
 *
 * Returns `true` if a spawn was attempted, `false` if the open could not
 * proceed — covers (a) URL rejected by `parseSafeUrl` (non-http(s),
 * malformed, etc.), (b) no known opener for the current platform, or (c)
 * every `spawn()` threw synchronously before a child was created. (b) and
 * (c) are also reported through `onOpenFailure`. Async failures after spawn
 * still return `true`; the error listener keeps the TUI alive.
 */
export function openExternalUrl(
  rawUrl: string,
  dependencies: OpenDependencies = {},
): boolean {
  const url = parseSafeUrl(rawUrl);

  if (!url) {
    return false;
  }

  const spawnFn = dependencies.spawn ?? spawn;
  const platformId = dependencies.platform?.() ?? platform();
  const commands = openCommands(platformId, dependencies.env ?? process.env);
  const report = dependencies.onFailure ?? emitOpenFailure;

  const fail = () => {
    const tried = commands.map((c) => c.command).join(", ");

    report(
      `could not open link${tried ? ` (tried ${tried})` : ": no opener for this platform"}: ${url.toString()}`,
    );
  };

  // Try opener `index`; on a sync throw or an async 'error' (binary missing,
  // EACCES) fall through to the next one. Returns whether a spawn was made.
  const attempt = (index: number): boolean => {
    const command = commands[index];

    if (!command) {
      fail();

      return false;
    }

    try {
      const child = spawnFn(
        command.command,
        [...command.args, url.toString()],
        {
          // Detach so closing the TUI later doesn't kill the browser process,
          // and ignore stdio so we don't leak FDs into our raw-mode terminal.
          // Without `ignore` here, Chrome's stderr can land in the alt screen.
          detached: true,
          stdio: "ignore",
        } satisfies SpawnOptions,
      );

      // spawn returns a ChildProcess synchronously even when the binary is
      // missing; the failure surfaces later as an 'error' event. Without a
      // handler an unhandled 'error' crashes Node and the whole TUI, so
      // attach one BEFORE unref(). Exit codes are ignored on purpose:
      // explorer.exe exits 1 even when it opened the URL.
      child.once("error", () => {
        attempt(index + 1);
      });

      child.unref();

      return true;
    } catch {
      // spawn can also throw synchronously on argv-validation failures
      // (e.g. NUL in the path); try the next opener.
      return attempt(index + 1);
    }
  };

  return attempt(0);
}

type OpenFailureListener = (message: string) => void;

const openFailureListeners = new Set<OpenFailureListener>();

/** Subscribe to "could not open link" reports; returns the unsubscribe. */
export function onOpenFailure(listener: OpenFailureListener): () => void {
  openFailureListeners.add(listener);

  return () => {
    openFailureListeners.delete(listener);
  };
}

function emitOpenFailure(message: string): void {
  for (const listener of openFailureListeners) {
    listener(message);
  }
}

export type OpenDependencies = {
  env?: NodeJS.ProcessEnv;
  onFailure?: OpenFailureListener;
  platform?: () => string;
  spawn?: typeof spawn;
};

/**
 * Validate and normalize a URL for opening externally.
 * Exported for testing.
 */
export function parseSafeUrl(value: string): null | URL {
  if (!value || typeof value !== "string") {
    return null;
  }

  let parsed: URL;

  try {
    parsed = new URL(value);
  } catch {
    return null;
  }

  // http(s) only — opening file://, data:, javascript:, vbscript:, etc.
  // would let a malicious model run a local handler with attacker-controlled
  // input on a single click.
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return null;
  }

  // Reject empty or all-whitespace hostnames defensively. URL parsing
  // accepts URLs like 'http:///foo' on some Node versions; we don't want
  // to forward those to `open`.
  if (!parsed.hostname.trim()) {
    return null;
  }

  return parsed;
}

type OpenCommand = { command: string; args: readonly string[] };

/**
 * Per-platform open command. We deliberately avoid `cmd.exe /c start` on
 * Windows even though it's the canonical example, because `start` is a cmd
 * builtin: the URL string is reparsed by cmd's command-line tokenizer and
 * characters like `&`, `|`, `^`, `<`, `>` either break the command or get
 * interpreted as additional commands. That undermines the protocol
 * allowlist's safety story and also breaks plain http(s) URLs with `&` in
 * query strings. `explorer.exe <url>` is the safe, non-shell alternative —
 * it invokes the registered protocol handler for http(s) without going
 * through cmd. Linux/BSD use `xdg-open` directly with no shell wrapping.
 *
 * Returns null for platforms where we don't know a safe opener (e.g. `aix`,
 * `sunos`, `cygwin`). The caller's `if (!command) return false` path then
 * surfaces "no opener" instead of optimistically trying `xdg-open` on a
 * platform that probably doesn't have it.
 */
export function openCommand(platformId: string): OpenCommand | null {
  if (platformId === "darwin") {
    return { command: "open", args: [] };
  }

  if (platformId === "win32") {
    return { command: "explorer.exe", args: [] };
  }

  // Linux + the BSD family ship xdg-open via xdg-utils. Everything else
  // (aix, sunos, cygwin, haiku, etc.) returns null so openExternalUrl's
  // command-not-found fallback fires honestly.
  const XDG_OPEN_PLATFORMS = new Set([
    "linux",
    "freebsd",
    "openbsd",
    "netbsd",
    "dragonfly",
  ]);

  if (XDG_OPEN_PLATFORMS.has(platformId)) {
    return { command: "xdg-open", args: [] };
  }

  return null;
}

/**
 * Ordered openers to try. Same as `openCommand` except under WSL (platform
 * "linux" with WSL_INTEROP or WSL_DISTRO_NAME set, the same check
 * lib/clipboard.ts uses), where `wslview` and `explorer.exe` (both reach the
 * Windows browser, no shell involved) come before `xdg-open`.
 */
export function openCommands(
  platformId: string,
  env: NodeJS.ProcessEnv = process.env,
): OpenCommand[] {
  const base = openCommand(platformId);
  const commands: OpenCommand[] = base ? [base] : [];

  if (platformId === "linux" && (env.WSL_INTEROP || env.WSL_DISTRO_NAME)) {
    commands.unshift(
      { command: "wslview", args: [] },
      { command: "explorer.exe", args: [] },
    );
  }

  return commands;
}
