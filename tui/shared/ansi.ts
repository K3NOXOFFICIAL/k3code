// ANSI escape stripping shared by every TS surface (TUI, desktop, web).
// Covers CSI (complete and truncated tails), OSC (hyperlinks, titles), DCS/SOS/PM/APC
// strings, multi-byte non-CSI ESC sequences, stray ESC bytes and C0 controls — a
// weaker SGR-only regex leaves `]8;;url\x07` payloads and `[12;` tails visible.

const ESC = String.fromCharCode(27);
const BEL = String.fromCharCode(7);
const ST8 = String.fromCharCode(0x9c); // the 8-bit string terminator
const ANSI_CSI_RE = new RegExp(`${ESC}\\[[0-?]*[ -/]*[@-~]`, "g");
const ANSI_CSI_WITH_CMD_RE = new RegExp(`${ESC}\\[[0-?]*[ -/]*([@-~])`, "g");
const ANSI_INCOMPLETE_CSI_RE = new RegExp(
  `${ESC}\\[[0-?]*[ -/]*(?=${ESC}|\\n|$)`,
  "g",
);
// OSC and DCS/SOS/PM/APC strings end at BEL or ST (ESC \\). One that never ends (output cut off, a
// program killed mid-sequence) must not reach into the lines after it and eat everything up to the
// next BEL anywhere in the text, so the end of its line closes it too; real payloads (titles,
// hyperlinks, sixel) have no line breaks.
const ANSI_OSC_RE = new RegExp(
  `${ESC}\\][^\\n]*?(?:${BEL}|${ST8}|${ESC}\\\\|(?=\\n)|$)`,
  "g",
);
const ANSI_STRING_RE = new RegExp(
  `${ESC}[PX^_][^\\n]*?(?:${BEL}|${ST8}|${ESC}\\\\|(?=\\n)|$)`,
  "g",
);
const ANSI_NON_CSI_ESC_SEQ_RE = new RegExp(
  `${ESC}(?!\\[|\\]|P|X|\\^|_)[ -/]*[0-~]`,
  "g",
);
const ANSI_STRAY_ESC_RE = new RegExp(`${ESC}(?!\\[)[\\s\\S]?`, "g");
// eslint-disable-next-line no-control-regex -- intentionally strips C0/C1 control chars
const CONTROL_RE = /[\x00-\x08\x0B\x0C\x0D\x0E-\x1A\x1C-\x1F\x7F]/g;

// 8-bit C1 controls: U+009B is CSI, U+009D OSC, U+0090/98/9E/9F open DCS/SOS/PM/APC strings and
// U+009C (ST) closes them. A terminal in 8-bit mode runs them like the 7-bit forms, so they go the
// same way, whole (a bare C1 byte is removed too). Render input drops them rather than converting
// them: only 7-bit SGR is kept.
const C1_STRING_RE =
  /[\u0090\u0098\u009d\u009e\u009f][^\n]*?(?:\u0007|\u009c|\u001b\\|(?=\n)|$)/g;
const C1_CSI_RE = /\u009b[0-?]*[ -/]*[@-~]?/g;
const C1_RE = /[\u0080-\u009f]/g;

// Strings first (whatever opened or closes them), then the C1 sequences that are left.
const stripStringsAndC1 = (s: string) =>
  s
    .replace(ANSI_OSC_RE, "")
    .replace(ANSI_STRING_RE, "")
    .replace(C1_STRING_RE, "")
    .replace(C1_CSI_RE, "")
    .replace(C1_RE, "");

/** Remove every escape sequence and control byte, returning plain visible text. */
export const stripAnsi = (s: string) =>
  stripStringsAndC1(s)
    .replace(ANSI_INCOMPLETE_CSI_RE, "")
    .replace(ANSI_CSI_RE, "")
    .replace(ANSI_INCOMPLETE_CSI_RE, "")
    .replace(ANSI_NON_CSI_ESC_SEQ_RE, "")
    .replace(ANSI_STRAY_ESC_RE, "")
    .replace(CONTROL_RE, "");

/** Like stripAnsi but keeps SGR (`m`) sequences so a styled renderer can colorize. */
export const sanitizeAnsiForRender = (s: string) =>
  stripStringsAndC1(s)
    .replace(ANSI_INCOMPLETE_CSI_RE, "")
    .replace(ANSI_CSI_WITH_CMD_RE, (seq, cmd: string) =>
      cmd === "m" ? seq : "",
    )
    .replace(ANSI_INCOMPLETE_CSI_RE, "")
    .replace(ANSI_NON_CSI_ESC_SEQ_RE, "")
    .replace(ANSI_STRAY_ESC_RE, "")
    .replace(CONTROL_RE, "");

export const hasAnsi = (s: string) =>
  s.includes(ESC) || /[\u0080-\u009f]/.test(s);
