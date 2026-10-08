import { MIN_ANIMATION_TICK_MS } from './animation.js'
import { PET_CELEBRATION, PET_NAMES, PETS, type PetName, type PetStateName } from '../content/pets.js'

import { type Rng, workingMessage } from './workingLine.js'

/** The terminal pet: a three-line creature in the right-hand corner. This module
 *  is the pure part (phases, transitions, frames, picking, `/pet` parsing); the
 *  React component in `components/terminalPet.tsx` drives it. */

export type PetPhase = PetStateName

export type PetEvent = 'done_expired' | 'input_needed' | 'input_resolved' | 'turn_end' | 'turn_start'

/** How long the pet keeps its "done" pose before it goes back to idle. */
export const PET_DONE_HOLD_MS = 3000

/** Frame period. Never below the shared animation floor. */
export const PET_TICK_MS = Math.max(MIN_ANIMATION_TICK_MS, 400)

/** Art width in cells; every frame line is padded to this. */
export const PET_ART_WIDTH = 9

/** Corner column width: a left gutter, then the quip column under the art. */
export const PET_COLUMN_WIDTH = 15

/** Quip column: the text under the pet while it works, and the celebration on a finished turn. */
export const PET_QUIP_WIDTH = PET_COLUMN_WIDTH - 1
export const PET_QUIP_ROWS = 3

/** Narrower terminals keep the full transcript width; the pet hides itself. */
export const PET_MIN_COLS = 100

/** `/pet party` shows three pets from this width; two from `PET_MIN_COLS`. */
export const PET_TRIO_MIN_COLS = 130

export const nextPetPhase = (phase: PetPhase, event: PetEvent): PetPhase => {
  switch (event) {
    case 'turn_start':
      return 'working'

    case 'turn_end':
      return phase === 'working' || phase === 'needs_input' ? 'done' : phase

    case 'input_needed':
      return 'needs_input'

    case 'input_resolved':
      return phase === 'needs_input' ? 'working' : phase

    case 'done_expired':
      return phase === 'done' ? 'idle' : phase
  }
}

/** The three lines to draw for pet `name` in `phase` at animation `tick`.
 *  Reduced motion pins frame 0. */
export const petLines = (name: PetName, phase: PetPhase, tick: number, reduced: boolean): string[] => {
  const frames = PETS[name][phase]
  const index = reduced ? 0 : ((tick % frames.length) + frames.length) % frames.length
  const frame = frames[index] ?? frames[0]!

  return frame.map(line => line.padEnd(PET_ART_WIDTH))
}

/** How many pets the corner shows. A party needs `PET_MIN_COLS` for two pets and
 *  `PET_TRIO_MIN_COLS` for three; on a narrower terminal a party keeps one pet. A
 *  single pet is hidden below `PET_MIN_COLS`. */
export const petCrewSize = (enabled: boolean, cols: number, party = false): number => {
  if (!enabled) {
    return 0
  }

  if (cols >= PET_MIN_COLS) {
    return party ? (cols >= PET_TRIO_MIN_COLS ? 3 : 2) : 1
  }

  return party ? 1 : 0
}

/** Columns the pets reserve in the transcript row (0 when hidden). */
export const petColumnWidth = (enabled: boolean, cols: number, party = false): number =>
  petCrewSize(enabled, cols, party) * PET_COLUMN_WIDTH

/** The pets in the corner, left to right: `lead`, then the names after it in
 *  `PET_NAMES` order (wrapping). Deterministic, so a party does not reshuffle on render. */
export const petCrew = (lead: PetName, size: number, names: readonly PetName[] = PET_NAMES): PetName[] => {
  const start = Math.max(0, names.indexOf(lead))

  return Array.from({ length: Math.min(size, names.length) }, (_, i) => names[(start + i) % names.length]!)
}

/** Quip text under the pet, word-wrapped to `width` in at most `rows` lines. Text
 *  that does not fit ends in "...". Each line is padded to `width`. */
export const wrapQuip = (text: string, width = PET_QUIP_WIDTH, rows = PET_QUIP_ROWS): string[] => {
  const lines: string[] = []
  let line = ''

  for (const word of text.split(/\s+/).filter(Boolean)) {
    const joined = line ? `${line} ${word}` : word

    if (joined.length <= width) {
      line = joined

      continue
    }

    if (line) {
      lines.push(line)
    }

    // A word wider than the column is cut across lines.
    line = word

    while (line.length > width) {
      lines.push(line.slice(0, width))
      line = line.slice(width)
    }
  }

  if (line) {
    lines.push(line)
  }

  if (lines.length > rows) {
    lines.length = rows
    lines[rows - 1] = `${(lines[rows - 1] ?? '').slice(0, width - 3)}...`
  }

  return Array.from({ length: rows }, (_, i) => (lines[i] ?? '').padEnd(width))
}

/** The quip rows when the pet has nothing to say. */
export const blankQuip = (): string[] => Array.from({ length: PET_QUIP_ROWS }, () => ''.padEnd(PET_QUIP_WIDTH))

/** The celebration rows for a finished turn. Reduced motion pins the first frame. */
export const celebrationLines = (tick: number, reduced: boolean): string[] => {
  const index = reduced ? 0 : ((tick % PET_CELEBRATION.length) + PET_CELEBRATION.length) % PET_CELEBRATION.length

  return (PET_CELEBRATION[index] ?? PET_CELEBRATION[0]!).map(line => line.padEnd(PET_QUIP_WIDTH))
}

/** The quip shown while the pet works: a working-line message, wrapped for the column. */
export const quipLines = (index: number): string[] => wrapQuip(workingMessage(index))

const indexFrom = (rng: Rng, count: number) => Math.min(count - 1, Math.floor(rng() * count))

/** A pet name chosen at random. The RNG is injectable so tests are deterministic. */
export const pickPet = (rng: Rng = Math.random, names: readonly PetName[] = PET_NAMES): PetName =>
  names[indexFrom(rng, names.length)] ?? names[0]!

/** A random pet other than `current` (falls back to `current` when there is no other). */
export const pickOtherPet = (
  current: PetName,
  rng: Rng = Math.random,
  names: readonly PetName[] = PET_NAMES
): PetName => {
  const others = names.filter(name => name !== current)

  return others.length ? pickPet(rng, others) : current
}

export const isPetName = (value: string): value is PetName => (PET_NAMES as readonly string[]).includes(value)

export interface PetSettings {
  enabled: boolean
  name: PetName
  /** `/pet party` is on: the pet and its neighbours show side by side. */
  party?: boolean
}

export interface PetCommandResult {
  /** New on/off state, when the command changes it. */
  enabled?: boolean
  /** New pet, when the command changes it. */
  name?: PetName
  /** New party state, when the command changes it. */
  party?: boolean
  /** Transcript line to show. */
  message: string
}

export const PET_USAGE = `usage: /pet [on|off|toggle|status|random|party|solo|${PET_NAMES.join('|')}]`

/** `/pet` with no argument reports. `on` / `off` / `toggle` set visibility. `random`
 *  picks a different pet. A pet name chooses that pet and shows it. */
export const parsePetCommand = (arg: string, current: PetSettings, rng: Rng = Math.random): PetCommandResult => {
  const word = arg.trim().toLowerCase()

  if (!word || word === 'status') {
    return {
      message: `pet: ${current.enabled ? 'on' : 'off'}, ${current.name}${current.party ? ', party' : ''}`
    }
  }

  if (word === 'party') {
    return { enabled: true, party: true, message: 'pet party' }
  }

  if (word === 'solo') {
    return { party: false, message: 'pet solo' }
  }

  if (word === 'on' || word === 'off') {
    const enabled = word === 'on'

    return { enabled, message: `pet ${word}` }
  }

  if (word === 'toggle') {
    return {
      enabled: !current.enabled,
      message: `pet ${current.enabled ? 'off' : 'on'}`
    }
  }

  if (word === 'random') {
    const name = pickOtherPet(current.name, rng)

    return { enabled: true, message: `pet: ${name}`, name }
  }

  if (isPetName(word)) {
    return { enabled: true, message: `pet: ${word}`, name: word }
  }

  return { message: PET_USAGE }
}
