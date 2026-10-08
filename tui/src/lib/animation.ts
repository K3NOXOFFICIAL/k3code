/** Reduced motion: `K3_NO_ANIMATION=1` turns every timer-driven animation off and
 *  shows static glyphs instead. Read per call so a test (or a late env change)
 *  takes effect without re-importing the module. */
export const REDUCED_MOTION_ENV = "K3_NO_ANIMATION";

/** Floor for any animation timer. Faster ticks re-render for no visible gain
 *  and cost terminal bandwidth; the working line and pet never tick below this. */
export const MIN_ANIMATION_TICK_MS = 250;

const TRUTHY = new Set(["1", "true", "yes", "on"]);

export const isReducedMotion = (
  env: Readonly<Record<string, string | undefined>> = process.env,
): boolean =>
  TRUTHY.has(
    String(env[REDUCED_MOTION_ENV] ?? "")
      .trim()
      .toLowerCase(),
  );
