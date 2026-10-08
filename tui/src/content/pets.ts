/** Terminal pets: three-line ASCII art, one set of frames per state. Every frame
 *  line is plain ASCII and at most 9 cells wide (`PET_ART_WIDTH` in lib/terminalPet). */

export type PetStateName = 'done' | 'idle' | 'needs_input' | 'working'

export type PetFrame = readonly [string, string, string]

export type PetStates = Record<PetStateName, readonly PetFrame[]>

/** Names in display/pick order. */
export const PET_NAMES = ['blob', 'cat', 'crab', 'duck', 'ghost', 'hamster', 'owl', 'robot'] as const

export type PetName = (typeof PET_NAMES)[number]

const CRAB_OPEN: PetFrame = ['\\(o.o)/', ' (___)', ' ^   ^ ']
const CRAB_BLINK: PetFrame = ['\\(-.-)/', ' (___)', ' ^   ^ ']

const GHOST: PetFrame = [' .---. ', '( o o )', ' ~~~~~ ']
const GHOST_BLINK: PetFrame = [' .---. ', '( - - )', ' ~~~~~ ']

const ROBOT: PetFrame = [' [===] ', '|o   o|', ' |___| ']
const ROBOT_BLINK: PetFrame = [' [===] ', '|-   -|', ' |___| ']

const DUCK: PetFrame = ['  __  ', '<(o )__', " `--'  "]
const DUCK_BLINK: PetFrame = ['  __  ', '<(- )__', " `--'  "]

const OWL: PetFrame = [' {o,o}', ' /)__)', ' -"-"-']
const OWL_BLINK: PetFrame = [' {-,-}', ' /)__)', ' -"-"-']

const HAMSTER: PetFrame = ['  _  _ ', ' (o)(o)', ' ( ww )']
const HAMSTER_BLINK: PetFrame = ['  _  _ ', ' (-)(-)', ' ( ww )']

const BLOB: PetFrame = ['  .-.  ', ' (o o) ', " '---' "]
const BLOB_BLINK: PetFrame = ['  .-.  ', ' (- -) ', " '---' "]

const CAT: PetFrame = [' /\\_/\\', '( o.o )', ' > ^ < ']
const CAT_BLINK: PetFrame = [' /\\_/\\', '( -.- )', ' > ^ < ']

/** The burst under the pet when a turn finishes: two frames, alternating while the
 *  pet holds its done pose. Wider than the art (the quip column), still plain ASCII. */
export const PET_CELEBRATION: readonly PetFrame[] = [
  ['*  *  *  *', ' \\ | | / ', '  done!    '],
  [' *  *  *  ', ' / | | \\ ', '  done!    ']
]

export const PETS: Record<PetName, PetStates> = {
  blob: {
    done: [
      ['  .-.  ', ' (^ ^) ', " '---' "],
      ['  .-.  ', ' (^ ^)*', " '---' "]
    ],
    idle: [BLOB, BLOB, BLOB_BLINK],
    needs_input: [
      ['  .-.  ?', ' (o o) ', " '---' "],
      ['  .-.  !', ' (O O) ', " '---' "]
    ],
    working: [
      ['  .-.  ', ' (O O) ', " '---' "],
      [' .---. ', ' (O O) ', "'-----'"]
    ]
  },
  cat: {
    done: [
      [' /\\_/\\ *', '( ^w^ )', ' > ^ < '],
      [' /\\_/\\  ', '( ^w^ )/', ' > ^ < ']
    ],
    idle: [CAT, CAT, CAT_BLINK],
    needs_input: [
      [' /\\_/\\ ?', '( o.O )', ' > ^ < '],
      [' /\\_/\\ !', '( O.o )', ' > ^ < ']
    ],
    working: [
      [' /\\_/\\', '( ^.^ )', ' d   b '],
      [' /\\_/\\', '( ^.^ )', ' b   d ']
    ]
  },
  crab: {
    done: [
      ['\\(^.^)/', ' (___)', ' ^ ^ ^ '],
      ['\\(^o^)/', '  (_)  ', ' ^ ^ ^ ']
    ],
    idle: [CRAB_OPEN, CRAB_OPEN, CRAB_BLINK],
    needs_input: [
      ['\\(o.O)/?', ' (___)', ' ^   ^ '],
      ['\\(O.o)/!', ' (___)', ' ^   ^ ']
    ],
    working: [
      ['/(o.o)\\', ' (___)', ' ^^^^^ '],
      ['\\(o.o)/', ' (___)', ' ^^^^^ ']
    ]
  },
  duck: {
    done: [
      ['  __  ', '<(^ )__', " `--'  "],
      ['  __  ', '<(^ )>_', " `--'  "]
    ],
    idle: [DUCK, DUCK, DUCK_BLINK],
    needs_input: [
      ['  __  ?', '<(o )__', " `--'  "],
      ['  __  !', '<(O )__', " `--'  "]
    ],
    working: [
      ['  __  ', '<(o )__', " `--'  "],
      ['  __  ', '<(o )__', "  `--' "]
    ]
  },
  ghost: {
    done: [
      [' .---. ', '( ^ ^ )', ' ~~~~~ '],
      [' .---. ', '( ^ ^ )', '~~~~~~~']
    ],
    idle: [GHOST, GHOST, GHOST_BLINK],
    needs_input: [
      [' .---. ?', '( o o )', ' ~~~~~ '],
      [' .---. !', '( O O )', ' ~~~~~ ']
    ],
    working: [
      [' .---. ', '( @ @ )', ' ~^~^~ '],
      [' .---. ', '( @ @ )', ' ^~^~^ ']
    ]
  },
  hamster: {
    done: [
      ['  _  _ ', ' (^)(^)', ' ( ww )'],
      ['  _  _*', ' (^)(^)', ' ( WW )']
    ],
    idle: [HAMSTER, HAMSTER, HAMSTER_BLINK],
    needs_input: [
      ['  _  _?', ' (O)(O)', ' ( ww )'],
      ['  _  _!', ' (o)(o)', ' ( ww )']
    ],
    working: [
      ['  _  _ ', ' (o)(o)', ' ( ww )'],
      ['  _  _ ', ' (o)(o)', ' ( vv )']
    ]
  },
  owl: {
    done: [
      [' {^,^}', ' /)__)', ' -"-"-'],
      [' {^,^}', ' \\)__/', ' -"-"-']
    ],
    idle: [OWL, OWL, OWL_BLINK],
    needs_input: [
      [' {O,o}?', ' /)__)', ' -"-"-'],
      [' {o,O}!', ' /)__)', ' -"-"-']
    ],
    working: [
      [' {O,O}', ' \\)__/', ' -"-"-'],
      [' {O,O}', ' /)__)', ' -"-"-']
    ]
  },
  robot: {
    done: [
      [' \\[=]/ ', '|^   ^|', ' |___| '],
      [' [===] ', '|^   ^|', ' |___| ']
    ],
    idle: [ROBOT, ROBOT, ROBOT_BLINK],
    needs_input: [
      [' [===] ?', '|o   O|', ' |___| '],
      [' [===] !', '|O   o|', ' |___| ']
    ],
    working: [
      [' [===] ', '|O   O|', ' |_#_| '],
      [' [===] ', '|O   O|', ' |#_#| ']
    ]
  }
}
