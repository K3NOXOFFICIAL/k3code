export interface WaitForOptions {
  interval?: number;
  timeout?: number;
}

/**
 * Polls `check` until it stops throwing and does not return `false`, then resolves with its result.
 *
 * vitest's own `vi.waitFor` gives up after 1 s, which a loaded machine can exceed between a key press and the
 * re-render it triggers. This one waits up to 5 s by default and rejects with the last failure as the cause.
 */
export async function waitFor<T>(
  check: () => T | Promise<T>,
  { interval = 10, timeout = 5000 }: WaitForOptions = {},
): Promise<Exclude<T, false>> {
  // Counted in polls, not read from Date.now: suites pin Date.now (and may fake timers) for their own purposes.
  const attempts = Math.max(1, Math.ceil(timeout / interval));
  let last: unknown;

  for (let attempt = 1; ; attempt++) {
    try {
      const result = await check();

      if (result !== false) {
        return result as Exclude<T, false>;
      }

      last = new Error("condition returned false");
    } catch (error) {
      last = error;
    }

    if (attempt >= attempts) {
      const reason = last instanceof Error ? last.message : String(last);

      throw new Error(
        `waitFor: condition not met within ${timeout} ms: ${reason}`,
        {
          cause: last,
        },
      );
    }

    await new Promise((resolve) => setTimeout(resolve, interval));
  }
}
