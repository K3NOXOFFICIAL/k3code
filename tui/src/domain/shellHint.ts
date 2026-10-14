import { shortCwd } from "./paths.js";

/** The composer is in shell mode: the same test the submit path makes (`text.startsWith("!")`), so a leading space
 *  is a prompt, not a command. */
export const isShellInput = (text: string) => text.startsWith("!");

/** The dim row the composer shows in shell mode, cut to `width`; null for any other input (no row, no height). */
export const shellHintLine = (
  input: string,
  cwd: string,
  width: number,
): null | string => {
  if (!isShellInput(input)) {
    return null;
  }

  const line = `! shell · runs in ${shortCwd(cwd)} · output is shared with the model · Backspace leaves`;
  const w = Math.max(1, width);

  return line.length <= w ? line : `${line.slice(0, Math.max(0, w - 1))}…`;
};
