import { atom } from "nanostores";

import { DOUBLE_ESC_MS } from "../config/timing.js";

/** True while the first Esc of a mid-turn interrupting pair waits for its second: the working line then shows
 *  "Esc again to interrupt". Lapses with the pair's window; the input handler hides it early on any other key, the
 *  interrupt, the turn ending or a blocking overlay. */
export const $escInterruptHint = atom(false);

let lapse: null | ReturnType<typeof setTimeout> = null;

export const hideEscInterruptHint = () => {
  if (lapse) {
    clearTimeout(lapse);
    lapse = null;
  }

  if ($escInterruptHint.get()) {
    $escInterruptHint.set(false);
  }
};

/** Show the hint for one DOUBLE_ESC_MS window, restarting it if it is already up. */
export const showEscInterruptHint = () => {
  hideEscInterruptHint();
  $escInterruptHint.set(true);
  lapse = setTimeout(hideEscInterruptHint, DOUBLE_ESC_MS);
};
