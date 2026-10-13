import { once } from "node:events";
import { PassThrough } from "node:stream";

import { describe, expect, it } from "vitest";

import { readBoundedLines } from "../lib/boundedLines.js";

function harness(maxChars: number) {
  const input = new PassThrough();
  const lines: string[] = [];
  const oversize: { chars: number; head: string }[] = [];
  const stop = readBoundedLines(input, {
    maxChars,
    onLine: (line) => lines.push(line),
    onOversize: (chars, head) => oversize.push({ chars, head }),
  });

  return { input, lines, oversize, stop };
}

describe("readBoundedLines", () => {
  it("splits lines across chunks, strips CR, and flushes a trailing line on end", async () => {
    const { input, lines } = harness(1000);

    input.write("one\ntw");
    input.write("o\r\nthree");
    input.end();
    await once(input, "end");

    expect(lines).toEqual(["one", "two", "three"]);
  });

  it("reassembles multi-byte characters split across chunks", () => {
    const { input, lines } = harness(1000);
    const bytes = Buffer.from("héllo\n");

    input.write(bytes.subarray(0, 2));
    input.write(bytes.subarray(2));

    expect(lines).toEqual(["héllo"]);
  });

  it("drops an over-long line without holding it, then keeps reading", () => {
    const { input, lines, oversize } = harness(100);

    input.write("ok\n");
    input.write("A".repeat(60));
    input.write("B".repeat(60));
    input.write("C".repeat(5000));
    input.write("\nafter\n");

    expect(lines).toEqual(["ok", "after"]);
    expect(oversize).toHaveLength(1);
    expect(oversize[0].chars).toBe(5120);
    expect(oversize[0].head.startsWith("AAAA")).toBe(true);
    expect(oversize[0].head.length).toBeLessThanOrEqual(240);
  });

  it("reports an over-long final line that ends the stream", async () => {
    const { input, lines, oversize } = harness(10);

    input.write("x".repeat(50));
    input.end();
    await once(input, "end");

    expect(lines).toEqual([]);
    expect(oversize).toEqual([{ chars: 50, head: "x".repeat(50) }]);
  });

  it("accepts a line of exactly maxChars", () => {
    const { input, lines, oversize } = harness(10);

    input.write("y".repeat(10) + "\n");

    expect(lines).toEqual(["y".repeat(10)]);
    expect(oversize).toEqual([]);
  });

  it("stops delivering after the returned stop function is called", () => {
    const { input, lines, stop } = harness(100);

    input.write("a\n");
    stop();
    input.write("b\n");

    expect(lines).toEqual(["a"]);
  });
});
