import { atom } from 'nanostores'

import type { PetName } from '../content/pets.js'
import { pickPet } from '../lib/terminalPet.js'

/** Terminal pet state, in memory for this session only (no file writes).
 *  `/pet on|off|toggle` sets visibility; `/pet <name>` and `/pet random` set the pet. */
export const $petEnabled = atom(true)

/** Chosen at random when the TUI starts. */
export const $petName = atom<PetName>(pickPet())

export const setPetEnabled = (enabled: boolean) => $petEnabled.set(enabled)

export const setPetName = (name: PetName) => $petName.set(name)
