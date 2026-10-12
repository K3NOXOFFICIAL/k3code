import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React, { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { patchUiState, resetUiState } from "../app/uiStore.js";
import {
  looksLikeDroppedPath,
  useComposerState,
} from "../app/useComposerState.js";
import type { GatewayClient } from "../gatewayClient.js";
import type * as clipboardModule from "../lib/clipboard.js";

const clipboard = vi.hoisted(() => ({ text: "" }));

// Ctrl+V reads the system clipboard; the test says what is on it.
vi.mock("../lib/clipboard.js", async (importOriginal) => ({
  ...(await importOriginal<typeof clipboardModule>()),
  readClipboardText: vi.fn(async () => clipboard.text),
}));

describe("looksLikeDroppedPath", () => {
  it("recognizes macOS screenshot temp paths and file URIs", () => {
    expect(
      looksLikeDroppedPath(
        "/var/folders/x/T/TemporaryItems/Screenshot\\ 2026-04-21\\ at\\ 1.04.43 PM.png",
      ),
    ).toBe(true);
    expect(
      looksLikeDroppedPath(
        "file:///var/folders/x/T/TemporaryItems/Screenshot%202026-04-21%20at%201.04.43%20PM.png",
      ),
    ).toBe(true);
  });

  it("rejects normal multiline or plain text paste", () => {
    expect(looksLikeDroppedPath("hello world")).toBe(false);
    expect(looksLikeDroppedPath("line one\nline two")).toBe(false);
  });

  it("recognizes paths with spaces (not backslash-escaped)", () => {
    expect(
      looksLikeDroppedPath(
        "/var/folders/x/T/TemporaryItems/Screenshot 2026-04-21 at 1.04.43 PM.png",
      ),
    ).toBe(true);
  });

  it("rejects empty/whitespace-only input", () => {
    expect(looksLikeDroppedPath("")).toBe(false);
    expect(looksLikeDroppedPath("   ")).toBe(false);
    expect(looksLikeDroppedPath("\n")).toBe(false);
  });

  it("rejects URLs that are not file:// URIs", () => {
    expect(looksLikeDroppedPath("https://example.com/image.png")).toBe(false);
    expect(looksLikeDroppedPath("http://localhost/file.pdf")).toBe(false);
  });

  it("rejects short slash-like strings without path structure", () => {
    // No second '/' or '.' → not a plausible file path
    expect(looksLikeDroppedPath("/help")).toBe(false);
    expect(looksLikeDroppedPath("/model sonnet")).toBe(false);
    expect(looksLikeDroppedPath("/api")).toBe(false);
  });

  it("accepts absolute paths with directory separators or extensions", () => {
    expect(looksLikeDroppedPath("/usr/bin/test")).toBe(true);
    expect(looksLikeDroppedPath("/tmp/file.txt")).toBe(true);
    expect(looksLikeDroppedPath("/etc/hosts")).toBe(true); // has second /
  });
});

// The real hook, to paste into
function mountComposer() {
  let api: null | ReturnType<typeof useComposerState> = null;
  const gw = { request: vi.fn(() => Promise.resolve({})) };

  function Probe() {
    const composer = useComposerState({
      gw: gw as unknown as GatewayClient,
      submitRef: { current: () => {} },
      sys: vi.fn(),
    });

    useEffect(() => {
      api = composer;
    });

    return null;
  }

  const stream = () =>
    Object.assign(new PassThrough(), { columns: 80, isTTY: false, rows: 24 });

  renderSync(React.createElement(Probe), {
    patchConsole: false,
    stderr: stream() as unknown as NodeJS.WriteStream,
    stdin: stream() as unknown as NodeJS.ReadStream,
    stdout: stream() as unknown as NodeJS.WriteStream,
  });

  return async () => {
    await vi.waitFor(() => expect(api).toBeTruthy());

    return api!;
  };
}

describe("a Ctrl+V clipboard with Windows line endings", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  const pasteFromClipboard = async (text: string) => {
    vi.stubEnv("K3CODE_HOME", "/nonexistent-k3code-home");
    // over SSH Ctrl+V waits for the terminal's OSC 52 answer instead of reading the clipboard: a local terminal here
    for (const name of ["SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"]) {
      vi.stubEnv(name, "");
    }
    resetUiState();
    patchUiState({ sid: "sess-1" });
    clipboard.text = text;

    const composer = await mountComposer()();

    return {
      composer,
      result: await composer.actions.handleTextPaste({
        cursor: 0,
        hotkey: true,
        text: "",
        value: "",
      }),
    };
  };

  it("is pasted without the carriage returns", async () => {
    const { result } = await pasteFromClipboard("a\r\n");

    expect(result).toEqual({ cursor: 1, value: "a" });
  });

  it("collapses with LF line endings, so the token carries the text the agent should read", async () => {
    const { composer, result } = await pasteFromClipboard(
      "l1\r\nl2\r\nl3\r\nl4\r\nl5\r\n",
    );

    expect(result?.value).toContain("[5 lines]");
    expect(composer.refs.tokensRef.current).toMatchObject([
      { kind: "paste", text: "l1\nl2\nl3\nl4\nl5" },
    ]);
  });

  it("treats a bare CR like a newline", async () => {
    const { result } = await pasteFromClipboard("a\rb\r");

    expect(result).toEqual({ cursor: 3, value: "a\nb" });
  });
});
