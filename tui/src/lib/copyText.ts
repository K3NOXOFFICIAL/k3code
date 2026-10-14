import { writeClipboardText } from "./clipboard.js";
import { writeOsc52Clipboard } from "./osc52.js";
import { isRemoteShellSession } from "./terminalSetup.js";

/**
 * Copy text to the user's clipboard. Remote shells use the terminal's OSC52
 * escape sequence only; local sessions try the native clipboard first and fall
 * back to OSC52. Resolves with the path taken; rejects on a real failure.
 */
export async function copyText(text: string): Promise<"native" | "osc52"> {
  if (isRemoteShellSession(process.env)) {
    writeOsc52Clipboard(text);

    return "osc52";
  }

  if (await writeClipboardText(text)) {
    return "native";
  }

  writeOsc52Clipboard(text);

  return "osc52";
}
