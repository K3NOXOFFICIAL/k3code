import { describe, expect, it } from "vitest";

import { hasAnsi, sanitizeAnsiForRender, stripAnsi } from "./ansi";

const ESC = String.fromCharCode(27);
const BEL = String.fromCharCode(7);

describe("stripAnsi", () => {
  it("strips CSI/OSC/control bytes from plain previews", () => {
    const sample = `A${ESC}[31mB${ESC}[39m${ESC}[2J${ESC}]0;title${BEL}C${ESC}[?25lD`;

    expect(stripAnsi(sample)).toBe("ABCD");
  });

  it("strips incomplete CSI prefixes and carriage returns", () => {
    const sample = `A${ESC}[31mB${ESC}[12;${ESC}[CD\rE`;

    expect(stripAnsi(sample)).toBe("ABDE");
  });

  it("strips multi-byte non-CSI ESC sequences without leaving trailing bytes", () => {
    const sample = `A${ESC}(0B${ESC}%GC${ESC})0D`;

    expect(stripAnsi(sample)).toBe("ABCD");
    expect(sanitizeAnsiForRender(sample)).toBe("ABCD");
  });

  // Desktop's former SGR-only stripper left OSC-8 hyperlink payloads, DCS strings and
  // truncated CSI tails visible in chat system messages; every surface now gets the
  // full coverage.
  it("leaves only visible text when OSC-8 hyperlinks, DCS strings, partial CSI and SGR mix", () => {
    const sample = `${ESC}]8;;https://example.com${BEL}link${ESC}]8;;${BEL} ${ESC}Pq#0${ESC}\\${ESC}[1;32mok${ESC}[0m${ESC}[12;`;

    const stripped = stripAnsi(sample);

    expect(stripped).toBe("link ok");
    expect(stripped).not.toContain(ESC);
    expect(stripped).not.toContain(BEL);
  });
});

// 8-bit C1 controls (U+009B CSI, U+009D OSC, U+009C ST, ...) are what a terminal in 8-bit
// mode reads as the 7-bit sequences. They used to survive and leave "31m" behind.
describe("C1 controls", () => {
  const CSI = String.fromCharCode(0x9b);
  const OSC = String.fromCharCode(0x9d);
  const DCS = String.fromCharCode(0x90);
  const APC = String.fromCharCode(0x9f);
  const ST = String.fromCharCode(0x9c);

  it("strips an 8-bit CSI sequence whole", () => {
    expect(stripAnsi(`A${CSI}31mB${CSI}0mC`)).toBe("ABC");
    expect(stripAnsi(`A${CSI}2JB`)).toBe("AB");
  });

  it("strips a truncated 8-bit CSI tail", () => {
    expect(stripAnsi(`A${CSI}12;`)).toBe("A");
    expect(stripAnsi(`A${CSI}`)).toBe("A");
  });

  it("strips 8-bit OSC, DCS and APC strings with BEL, ST or ESC \\ as terminator", () => {
    expect(stripAnsi(`${OSC}0;title${BEL}C`)).toBe("C");
    expect(
      stripAnsi(`${OSC}8;;https://example.com${ST}link${OSC}8;;${ST}`),
    ).toBe("link");
    expect(stripAnsi(`a${DCS}q#0${ST}b${APC}payload${ESC}\\c`)).toBe("abc");
  });

  it("strips a 7-bit string closed by an 8-bit ST, and a lone C1 byte", () => {
    expect(stripAnsi(`a${ESC}]0;t${ST}b`)).toBe("ab");
    expect(stripAnsi("a\u0085b\u0080c")).toBe("abc");
  });

  it("removes C1 sequences from render input too, keeping only 7-bit SGR", () => {
    expect(
      sanitizeAnsiForRender(`A${CSI}31mB${ESC}[32mC${OSC}0;t${BEL}D`),
    ).toBe(`AB${ESC}[32mCD`);
  });

  it("hasAnsi sees C1 controls", () => {
    expect(hasAnsi(`x${CSI}31m`)).toBe(true);
    expect(hasAnsi("x\u0085")).toBe(true);
  });
});

// An OSC/DCS that never ends (cut-off output, a program that died mid-sequence) swallowed
// every following line up to the next BEL or ST anywhere in the text.
describe("unterminated strings", () => {
  it("ends an unterminated OSC at the end of its line", () => {
    expect(stripAnsi(`a${ESC}]0;title\nb\nc${BEL}d`)).toBe("a\nb\ncd");
    expect(stripAnsi(`x${ESC}]0;ti`)).toBe("x");
  });

  it("ends an unterminated DCS/APC/PM/SOS at the end of its line", () => {
    for (const intro of ["P", "X", "^", "_"]) {
      expect(stripAnsi(`a${ESC}${intro}payload\nb${BEL}`)).toBe("a\nb");
    }
  });

  it("still strips a terminated string whole", () => {
    expect(stripAnsi(`a${ESC}]0;title${BEL}b`)).toBe("ab");
    expect(stripAnsi(`a${ESC}]8;;http://x${ESC}\\b`)).toBe("ab");
  });

  it("sanitizeAnsiForRender bounds it the same way", () => {
    expect(sanitizeAnsiForRender(`a${ESC}]0;title\nb\nc${BEL}d`)).toBe(
      "a\nb\ncd",
    );
  });
});

describe("sanitizeAnsiForRender", () => {
  it("keeps SGR color spans but removes cursor controls for Ansi rendering", () => {
    const sample = `A${ESC}[31mB${ESC}[39m${ESC}[2J${ESC}]0;title${BEL}${ESC}[?25lC`;

    expect(sanitizeAnsiForRender(sample)).toBe(`A${ESC}[31mB${ESC}[39mC`);
  });

  it("keeps valid SGR while removing dangling CSI and carriage returns", () => {
    const sample = `A${ESC}[31mB${ESC}[12;${ESC}[39mC\rD`;

    expect(sanitizeAnsiForRender(sample)).toBe(`A${ESC}[31mB${ESC}[39mCD`);
  });
});

describe("hasAnsi", () => {
  it("detects non-CSI escape prefixes too", () => {
    expect(hasAnsi(`ok${ESC}Ppayload${ESC}\\`)).toBe(true);
    expect(hasAnsi("plain")).toBe(false);
  });
});
