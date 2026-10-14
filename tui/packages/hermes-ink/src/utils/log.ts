export function logError(error: unknown): void {
  if (!(
    process.env.K3CODE_INK_DEBUG_ERRORS || process.env.HERMES_INK_DEBUG_ERRORS
  )) {
    return;
  }

  console.error(error);
}
