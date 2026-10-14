// Benchmark: memory and CPU of a burst of tool calls in the live area (#164).
//
// Mounts the real StreamingAssistant on a null terminal, then feeds N
// tool.start + tool.complete events through the real turnController, the way
// the gateway event handler does for a model that fans out many `echo` calls.
// Events are separated by one macrotask, like gateway lines read off a pipe,
// so every event gets its own React/Ink render of the whole live area.
//
// Each N runs in its own child process so one run's heap cannot skew another.
// Output is one JSON line per N: events, wallMs, cpuMs, peakRssMb (sampled
// from process.memoryUsage after every event), rssMb and heapMb after a forced
// GC (needs --expose-gc, which the orchestrator passes).
//
// NODE_ENV=production is forced for the child: dist/entry.js is built that way
// and React's dev build adds its own per-render bookkeeping.
//
// Run:          npx tsx scripts/bench-tool-burst.tsx            (20 40 80 160)
// Single case:  NODE_ENV=production node --expose-gc --import tsx scripts/bench-tool-burst.tsx <tools>

import { execFileSync } from "child_process";
import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React from "react";

import { turnController } from "../src/app/turnController.js";
import { resetTurnState } from "../src/app/turnStore.js";
import { StreamingAssistant } from "../src/components/streamingAssistant.js";

const MB = 1024 * 1024;

// isTTY matters: a non-TTY stdout makes Ink repaint the whole frame on every
// update (log-update.ts renderFullFrame), which is not what a terminal does.
const nullStream = () => {
  const s = new PassThrough();

  Object.assign(s, { columns: 120, isTTY: true, rows: 40 });
  s.on("data", () => {});

  return s;
};

const tick = () => new Promise<void>((r) => setImmediate(r));

const runCase = async (tools: number) => {
  resetTurnState();
  turnController.reset();

  const instance = renderSync(
    <StreamingAssistant
      cols={120}
      detailsMode="collapsed"
      detailsModeCommandOverride={false}
      progress={{ showProgressArea: true }}
    />,
    {
      patchConsole: false,
      stderr: nullStream() as unknown as NodeJS.WriteStream,
      stdin: nullStream() as unknown as NodeJS.ReadStream,
      stdout: nullStream() as unknown as NodeJS.WriteStream,
    },
  );

  const baseRss = process.memoryUsage().rss;
  const cpu0 = process.cpuUsage();
  const t0 = performance.now();
  let peak = baseRss;
  let events = 0;

  const sample = () => {
    events++;
    peak = Math.max(peak, process.memoryUsage().rss);
  };

  // BURST=pairs (default): start, complete, start, complete ... like a model
  // that runs its calls one after the other. BURST=parallel: all starts, then
  // all completes, like one assistant turn that fans out N calls at once.
  const parallel = process.env.BURST === "parallel";

  const start = async (i: number) => {
    turnController.recordToolStart(
      `t${i}`,
      "terminal",
      `echo ${i}`,
      `{"command":"echo ${i}"}`,
    );
    sample();
    await tick();
  };

  const complete = async (i: number) => {
    turnController.recordToolComplete(
      `t${i}`,
      "terminal",
      `echo ${i}`,
      0.1,
      undefined,
      `{"output":"${i}","exit_code":0}`,
    );
    sample();
    await tick();
  };

  if (parallel) {
    for (let i = 0; i < tools; i++) await start(i);
    for (let i = 0; i < tools; i++) await complete(i);
  } else {
    for (let i = 0; i < tools; i++) {
      await start(i);
      await complete(i);
    }
  }

  const wallMs = performance.now() - t0;
  const cpu = process.cpuUsage(cpu0);

  instance.unmount();
  globalThis.gc?.();

  const after = process.memoryUsage();

  return {
    cpuMs: Math.round((cpu.user + cpu.system) / 1000),
    events,
    heapMb: Math.round(after.heapUsed / MB),
    peakRssMb: Math.round(peak / MB),
    rssMb: Math.round(after.rss / MB),
    startRssMb: Math.round(baseRss / MB),
    tools,
    wallMs: Math.round(wallMs),
  };
};

const arg = process.argv[2];

if (arg) {
  console.log(JSON.stringify(await runCase(Number(arg))));
  process.exit(0);
}

for (const n of [20, 40, 80, 160]) {
  const out = execFileSync(
    process.execPath,
    [
      "--expose-gc",
      "--import",
      "tsx",
      new URL(import.meta.url).pathname,
      String(n),
    ],
    { encoding: "utf8", env: { ...process.env, NODE_ENV: "production" } },
  );

  console.log(out.trim().split("\n").pop());
}
