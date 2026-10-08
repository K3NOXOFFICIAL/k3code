import { describe, expect, it } from "vitest";

import { PET_NAMES, PETS, type PetStateName } from "../content/pets.js";
import { MIN_ANIMATION_TICK_MS } from "../lib/animation.js";
import {
  blankQuip,
  celebrationLines,
  isPetName,
  nextPetPhase,
  PET_ART_WIDTH,
  PET_COLUMN_WIDTH,
  PET_DONE_HOLD_MS,
  PET_MIN_COLS,
  PET_QUIP_ROWS,
  PET_QUIP_WIDTH,
  PET_TICK_MS,
  PET_TRIO_MIN_COLS,
  type PetPhase,
  parsePetCommand,
  petColumnWidth,
  petCrew,
  petCrewSize,
  petLines,
  pickOtherPet,
  pickPet,
  type PetSettings,
  quipLines,
  wrapQuip,
} from "../lib/terminalPet.js";
import {
  advanceRotation,
  startRotation,
  type Rng,
  WORKING_ROTATE_MS,
} from "../lib/workingLine.js";
import { WORKING_MESSAGES } from "../content/workingMessages.js";

const STATES: PetStateName[] = ["idle", "working", "done", "needs_input"];

const fixed =
  (value: number): Rng =>
  () =>
    value;

describe("pet registry", () => {
  it("has between six and eight pets", () => {
    expect(PET_NAMES.length).toBeGreaterThanOrEqual(6);
    expect(PET_NAMES.length).toBeLessThanOrEqual(8);
    expect(new Set(PET_NAMES).size).toBe(PET_NAMES.length);
  });

  it.each(PET_NAMES.map((name) => [name]))(
    "%s has all four states with frames",
    (name) => {
      for (const state of STATES) {
        expect(
          PETS[name][state].length,
          `${name}/${state}`,
        ).toBeGreaterThanOrEqual(2);
      }
    },
  );

  it.each(PET_NAMES.map((name) => [name]))(
    "%s frames are three lines, ASCII and fit the art width",
    (name) => {
      for (const state of STATES) {
        for (const frame of PETS[name][state]) {
          expect(frame).toHaveLength(3);

          for (const line of frame) {
            expect(
              line.length,
              `${name}/${state}: "${line}"`,
            ).toBeLessThanOrEqual(PET_ART_WIDTH);
            expect(
              /^[\x20-\x7e]*$/.test(line),
              `${name}/${state}: non-ASCII`,
            ).toBe(true);
          }
        }
      }
    },
  );

  it("shows a different frame between the two frames of an animated state", () => {
    for (const name of PET_NAMES) {
      const [first, second] = PETS[name].working;

      expect(first).not.toEqual(second);
    }
  });
});

describe("pet state machine", () => {
  it("goes working on turn start, done on turn end, and back to idle after the hold", () => {
    let phase: PetPhase = "idle";
    phase = nextPetPhase(phase, "turn_start");
    expect(phase).toBe("working");
    phase = nextPetPhase(phase, "turn_end");
    expect(phase).toBe("done");
    phase = nextPetPhase(phase, "done_expired");
    expect(phase).toBe("idle");
  });

  it("needs input while asked, then resumes working", () => {
    let phase: PetPhase = nextPetPhase("idle", "turn_start");
    phase = nextPetPhase(phase, "input_needed");
    expect(phase).toBe("needs_input");
    phase = nextPetPhase(phase, "input_resolved");
    expect(phase).toBe("working");
  });

  it("a turn that ends while input is pending lands in done", () => {
    expect(nextPetPhase("needs_input", "turn_end")).toBe("done");
  });

  it("ignores events that do not apply to the current phase", () => {
    expect(nextPetPhase("idle", "turn_end")).toBe("idle");
    expect(nextPetPhase("idle", "done_expired")).toBe("idle");
    expect(nextPetPhase("working", "input_resolved")).toBe("working");
    expect(nextPetPhase("working", "done_expired")).toBe("working");
    expect(nextPetPhase("done", "input_resolved")).toBe("done");
  });

  it("holds done for a few seconds and ticks no faster than the animation floor", () => {
    expect(PET_DONE_HOLD_MS).toBeGreaterThanOrEqual(1000);
    expect(PET_TICK_MS).toBeGreaterThanOrEqual(MIN_ANIMATION_TICK_MS);
  });
});

describe("pet frames", () => {
  it("pads every line to the art width", () => {
    for (const line of petLines("cat", "idle", 0, false)) {
      expect(line).toHaveLength(PET_ART_WIDTH);
    }
  });

  it("cycles frames with the tick", () => {
    const frames = PETS.ghost.working.length;
    const first = petLines("ghost", "working", 0, false);
    const wrapped = petLines("ghost", "working", frames, false);
    const next = petLines("ghost", "working", 1, false);

    expect(wrapped).toEqual(first);
    expect(next).not.toEqual(first);
  });

  it("pins frame 0 under reduced motion, whatever the tick", () => {
    for (const name of PET_NAMES) {
      for (const state of STATES) {
        const expected = petLines(name, state, 0, true);

        expect(petLines(name, state, 17, true)).toEqual(expected);
      }
    }
  });
});

describe("random pick", () => {
  it("picks with the injected RNG, deterministically", () => {
    expect(pickPet(fixed(0))).toBe(PET_NAMES[0]);
    expect(pickPet(fixed(0.999999))).toBe(PET_NAMES[PET_NAMES.length - 1]);
    expect(pickPet(fixed(0.5))).toBe(
      PET_NAMES[Math.floor(0.5 * PET_NAMES.length)],
    );
  });

  it("pickOtherPet never returns the current pet", () => {
    for (const current of PET_NAMES) {
      for (const value of [0, 0.3, 0.7, 0.999]) {
        expect(pickOtherPet(current, fixed(value))).not.toBe(current);
      }
    }
  });

  it("recognises pet names", () => {
    expect(isPetName("crab")).toBe(true);
    expect(isPetName("dragon")).toBe(false);
  });
});

describe("/pet command", () => {
  const current: PetSettings = { enabled: true, name: "cat" };

  it("reports the current state with no argument", () => {
    expect(parsePetCommand("", current).message).toBe("pet: on, cat");
    expect(
      parsePetCommand("status", { enabled: false, name: "owl" }).message,
    ).toBe("pet: off, owl");
    expect(parsePetCommand("", current).enabled).toBeUndefined();
  });

  it("turns the pet on and off", () => {
    expect(
      parsePetCommand("on", { enabled: false, name: "cat" }),
    ).toMatchObject({ enabled: true, message: "pet on" });
    expect(parsePetCommand("off", current)).toMatchObject({
      enabled: false,
      message: "pet off",
    });
    expect(parsePetCommand("toggle", current)).toMatchObject({
      enabled: false,
    });
  });

  it("chooses a named pet and shows it", () => {
    expect(parsePetCommand("Ghost", { enabled: false, name: "cat" })).toEqual({
      enabled: true,
      message: "pet: ghost",
      name: "ghost",
    });
  });

  it("random picks a different pet", () => {
    const result = parsePetCommand("random", current, fixed(0));

    expect(result.name).toBeDefined();
    expect(result.name).not.toBe("cat");
    expect(result.enabled).toBe(true);
  });

  it("party shows the party, solo goes back to one pet, and status says so", () => {
    expect(parsePetCommand("party", { enabled: false, name: "cat" })).toEqual({
      enabled: true,
      party: true,
      message: "pet party",
    });
    expect(
      parsePetCommand("solo", { enabled: true, name: "cat", party: true }),
    ).toEqual({
      party: false,
      message: "pet solo",
    });
    expect(
      parsePetCommand("status", { enabled: true, name: "cat", party: true })
        .message,
    ).toBe("pet: on, cat, party");
  });

  it("answers unknown input with the usage line naming every pet", () => {
    const result = parsePetCommand("dragon", current);

    expect(result.enabled).toBeUndefined();
    expect(result.name).toBeUndefined();
    for (const name of PET_NAMES) {
      expect(result.message).toContain(name);
    }
  });

  it("hides the pet column on narrow terminals and when off", () => {
    expect(petColumnWidth(true, PET_MIN_COLS - 1)).toBe(0);
    expect(petColumnWidth(true, PET_MIN_COLS)).toBe(PET_COLUMN_WIDTH);
    expect(petColumnWidth(false, 200)).toBe(0);
  });
});

describe("party layout", () => {
  it("shows two pets from 100 columns and three from 130; a narrow party keeps one", () => {
    expect(petCrewSize(true, 80)).toBe(0);
    expect(petCrewSize(true, 100)).toBe(1);
    expect(petCrewSize(true, 100, true)).toBe(2);
    expect(petCrewSize(true, PET_TRIO_MIN_COLS - 1, true)).toBe(2);
    expect(petCrewSize(true, PET_TRIO_MIN_COLS, true)).toBe(3);
    expect(petCrewSize(true, 80, true)).toBe(1);
    expect(petCrewSize(false, 200, true)).toBe(0);
  });

  it("reserves one pet column each at 100 and at 80 columns", () => {
    expect(petColumnWidth(true, 100, true)).toBe(2 * PET_COLUMN_WIDTH);
    expect(petColumnWidth(true, 80, true)).toBe(PET_COLUMN_WIDTH);
    expect(petColumnWidth(true, 80)).toBe(0);
  });

  it("lines the party up from the chosen pet, wrapping through the list", () => {
    expect(petCrew("owl", 2)).toEqual(["owl", "robot"]);
    expect(petCrew("owl", 3)).toEqual(["owl", "robot", "blob"]);
    expect(petCrew("blob", 1)).toEqual(["blob"]);
  });
});

describe("quips and celebration", () => {
  it("wraps a message into the quip column, every row padded to its width", () => {
    const rows = wrapQuip("Consulting the rubber duck");

    expect(rows).toHaveLength(PET_QUIP_ROWS);
    for (const row of rows) {
      expect(row).toHaveLength(PET_QUIP_WIDTH);
    }
    expect(rows[0]!.trimEnd()).toBe("Consulting the");
    expect(rows[1]!.trimEnd()).toBe("rubber duck");
  });

  it('ends text that does not fit with "..." and cuts a word wider than the column', () => {
    const rows = wrapQuip(
      "Wrestling the semicolon gremlin and then some more words",
    );

    expect(rows).toHaveLength(PET_QUIP_ROWS);
    expect(rows[PET_QUIP_ROWS - 1]!.trimEnd().endsWith("...")).toBe(true);

    const long = wrapQuip("Supercalifragilisticexpialidocious");

    expect(long).toHaveLength(PET_QUIP_ROWS);
    expect(long[0]!.trimEnd()).toHaveLength(PET_QUIP_WIDTH);
    expect(long.every((row) => row.length === PET_QUIP_WIDTH)).toBe(true);
  });

  it("quips show messages from the working pool", () => {
    expect(quipLines(0)).toEqual(wrapQuip(WORKING_MESSAGES[0]!));
    expect(quipLines(WORKING_MESSAGES.length - 1)).toEqual(
      wrapQuip(WORKING_MESSAGES[WORKING_MESSAGES.length - 1]!),
    );
  });

  it("rotation never shows the same quip twice in a row", () => {
    for (const rng of [fixed(0), fixed(0.5), fixed(0.999999), Math.random]) {
      let rotation = startRotation(rng);

      for (let segment = 1; segment <= 200; segment++) {
        const next = advanceRotation(
          rotation,
          segment * WORKING_ROTATE_MS,
          rng,
        );

        expect(next.index).not.toBe(rotation.index);
        rotation = next;
      }
    }
  });

  it("celebration alternates frames, and reduced motion pins the first", () => {
    expect(celebrationLines(0, false)).not.toEqual(celebrationLines(1, false));
    expect(celebrationLines(0, true)).toEqual(celebrationLines(3, true));
    for (const row of celebrationLines(1, false)) {
      expect(row).toHaveLength(PET_QUIP_WIDTH);
    }
    expect(blankQuip()).toHaveLength(PET_QUIP_ROWS);
  });
});
