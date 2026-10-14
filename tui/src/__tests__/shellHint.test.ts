import { describe, expect, it } from "vitest";

import { isShellInput, shellHintLine } from "../domain/shellHint.js";

describe("composer shell hint", () => {
  it("shows for input the submit path runs as a shell command", () => {
    expect(isShellInput("!x")).toBe(true);
    expect(shellHintLine("!x", "/srv/app", 200)).toBe(
      "! shell · runs in /srv/app · output is shared with the model · Backspace leaves",
    );
    expect(shellHintLine("!", "/srv/app", 200)).not.toBeNull();
  });

  it("does not show for a prompt, a leading space, or empty input", () => {
    expect(isShellInput("x")).toBe(false);
    expect(isShellInput(" !x")).toBe(false);
    expect(shellHintLine("x", "/srv/app", 200)).toBeNull();
    expect(shellHintLine(" !x", "/srv/app", 200)).toBeNull();
    expect(shellHintLine("", "/srv/app", 200)).toBeNull();
  });

  it("fits the width and shortens the cwd", () => {
    const line = shellHintLine("!ls", "/srv/app", 30)!;

    expect(line).toHaveLength(30);
    expect(line.endsWith("…")).toBe(true);
    expect(shellHintLine("!ls", `/a/${"deep/".repeat(20)}repo`, 500)).toMatch(
      /runs in …[a-z/]{18}deep\/repo ·/,
    );
  });
});
