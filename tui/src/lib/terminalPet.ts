import { MIN_ANIMATION_TICK_MS } from './animation.js'
import { PET_NAMES, PETS, type PetName, type PetStateName } from '../content/pets.js'

import type { Rng } from './workingLine.js'

/** The terminal pet: a three-line creature in the right-hand corner. This module
 *  is the pure part (phases, transitions, frames, picking, `/pet` parsing); the
 *  React component in `components/terminalPet.tsx` drives it. */

export type PetPhase = PetStateName

export type PetEvent = 'done_expired' | 'input_needed' | 'input_resolved' | 'turn_end' | 'turn_start'

/** How long the pet keeps its "done" pose before it goes back to idle. */
export const PET_DONE_HOLD_MS = 3000

/** Frame period. Never below the shared animation floor. */
export const PET_TICK_MS = Math.max(MIN_ANIMATION_TICK_MS, 400)

/** An idle pet blinks once every this many ms (one slow timer), for this long. */
export const PET_IDLE_BLINK_EVERY_MS = 4500
export const PET_IDLE_BLINK_MS = 180

/** Art width in cells; every frame line is padded to this. */
export const PET_ART_WIDTH = 9

/** Corner column width: art, a left gap, and a right gutter. */
export const PET_COLUMN_WIDTH = PET_ART_WIDTH + 2

/** Narrower terminals keep the full transcript width; the pet hides itself. */
export const PET_MIN_COLS = 100

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

/** Columns the pet reserves in the transcript row (0 when hidden). */
export const petColumnWidth = (enabled: boolean, cols: number): number =>
  enabled && cols >= PET_MIN_COLS ? PET_COLUMN_WIDTH : 0

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
}

export interface PetCommandResult {
  /** New on/off state, when the command changes it. */
  enabled?: boolean
  /** New pet, when the command changes it. */
  name?: PetName
  /** Transcript line to show. */
  message: string
}

export const PET_USAGE = `usage: /pet [on|off|toggle|status|random|${PET_NAMES.join('|')}]`

/** `/pet` with no argument reports. `on` / `off` / `toggle` set visibility. `random`
 *  picks a different pet. A pet name chooses that pet and shows it. */
export const parsePetCommand = (arg: string, current: PetSettings, rng: Rng = Math.random): PetCommandResult => {
  const word = arg.trim().toLowerCase()

  if (!word || word === 'status') {
    return {
      message: `pet: ${current.enabled ? 'on' : 'off'}, ${current.name}`
    }
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
