import { atom } from 'nanostores'

import type { PetName } from '../content/pets.js'
import { isPetName, pickPet } from '../lib/terminalPet.js'

/** Terminal pet state. `/pet on|off|toggle` sets visibility; `/pet <name>` and `/pet random`
 *  set the pet. The choice is saved as `display.pet` (off, on, or a pet name) through the
 *  gateway and applied again on the next launch by `applyPetConfig`. */
export const $petEnabled = atom(true)

/** Chosen at random when the TUI starts. */
export const $petName = atom<PetName>(pickPet())

export const setPetEnabled = (enabled: boolean) => $petEnabled.set(enabled)

export const setPetName = (name: PetName) => $petName.set(name)

/** `display.pet` from config: `off` hides the pet, a pet name pins it, anything else keeps
 *  the pet on with the species picked at launch. */
export const applyPetConfig = (raw: unknown) => {
  const value = String(raw ?? '')
    .trim()
    .toLowerCase()

  if (value === 'off') {
    $petEnabled.set(false)

    return
  }

  $petEnabled.set(true)

  if (isPetName(value)) {
    $petName.set(value)
  }
}

/** What to save after `/pet`: a named pet is pinned; random and on just mean "on". */
export const petConfigValue = (enabled: boolean, pinned: null | string): string =>
  !enabled ? 'off' : pinned && isPetName(pinned) ? pinned : 'on'
