import { Box, Text } from '@k3code/ink'
import { useStore } from '@nanostores/react'
import { useEffect, useReducer, useRef, useState } from 'react'

import { $overlayState } from '../app/overlayStore.js'
import { $petEnabled, $petName, $petParty } from '../app/petStore.js'
import type { PetName } from '../content/pets.js'
import { isReducedMotion } from '../lib/animation.js'
import {
  blankQuip,
  celebrationLines,
  nextPetPhase,
  PET_COLUMN_WIDTH,
  PET_DONE_HOLD_MS,
  PET_TICK_MS,
  type PetPhase,
  petCrew,
  petCrewSize,
  petLines,
  quipLines
} from '../lib/terminalPet.js'
import { advanceRotation, startRotation, type MessageRotation } from '../lib/workingLine.js'
import type { Theme } from '../theme.js'

/** The pet corner: one pet, or a party of two or three side by side on a wide terminal.
 *  Renders nothing when the pet is off or the terminal is too narrow for it. */
export function PetCorner({ busy, cols, t }: { busy: boolean; cols: number; t: Theme }) {
  const enabled = useStore($petEnabled)
  const name = useStore($petName)
  const party = useStore($petParty)
  const overlay = useStore($overlayState)
  const size = petCrewSize(enabled, cols, party)

  if (size === 0) {
    return null
  }

  const needsInput = Boolean(overlay.approval || overlay.clarify || overlay.confirm || overlay.secret || overlay.sudo)

  return (
    <Box flexDirection="row" flexShrink={0} justifyContent="flex-end" width={size * PET_COLUMN_WIDTH}>
      {petCrew(name, size).map(member => (
        <Box
          flexDirection="column"
          flexShrink={0}
          justifyContent="flex-end"
          key={member}
          paddingLeft={1}
          width={PET_COLUMN_WIDTH}
        >
          <TerminalPet busy={busy} name={member} needsInput={needsInput} t={t} />
        </Box>
      ))}
    </Box>
  )
}

const random = () => Math.random()

/** Animated pet with its quip column under the art. While working it says a
 *  message from the working pool (rotated through the turn); when the turn
 *  finishes it shows a celebration for the done hold. An idle pet arms no timer.
 *  Its timer runs only while mounted and doing something, so unmounting clears it. */
export function TerminalPet({
  busy,
  name,
  needsInput,
  t
}: {
  busy: boolean
  name: PetName
  needsInput: boolean
  t: Theme
}) {
  const [reduced] = useState(() => isReducedMotion())
  // A pet that mounts during a turn starts in `working` at once, not one render later.
  const [phase, dispatch] = useReducer(nextPetPhase, (busy ? 'working' : 'idle') as PetPhase)
  const [tick, setTick] = useState(0)
  // The quip: restarted at each turn, advanced every few seconds of that turn.
  const [quip, setQuip] = useState<MessageRotation>(() => startRotation(random))
  const turnStart = useRef(Date.now())
  const busyRef = useRef(busy)
  const needsRef = useRef(false)

  useEffect(() => {
    if (busy !== busyRef.current) {
      busyRef.current = busy

      if (busy) {
        turnStart.current = Date.now()
        setQuip(startRotation(random))
      }

      dispatch(busy ? 'turn_start' : 'turn_end')
    }
  }, [busy])

  useEffect(() => {
    if (needsInput !== needsRef.current) {
      needsRef.current = needsInput
      dispatch(needsInput ? 'input_needed' : 'input_resolved')
    }
  }, [needsInput])

  useEffect(() => {
    if (phase !== 'done') {
      return
    }

    const id = setTimeout(() => dispatch('done_expired'), PET_DONE_HOLD_MS)

    return () => clearTimeout(id)
  }, [phase])

  // Animate only while the pet is doing something. An idle pet is still: no timer.
  const animating = !reduced && phase !== 'idle'

  useEffect(() => {
    if (!animating) {
      return
    }

    const id = setInterval(() => {
      setTick(n => n + 1)
      setQuip(prev => advanceRotation(prev, Date.now() - turnStart.current, random))
    }, PET_TICK_MS)

    return () => clearInterval(id)
  }, [animating])

  const color = phase === 'needs_input' ? t.color.warn : phase === 'done' ? t.color.accent : t.color.muted
  const quipRows =
    phase === 'working' ? quipLines(quip.index) : phase === 'done' ? celebrationLines(tick, reduced) : blankQuip()

  return (
    <Box flexDirection="column">
      {petLines(name, phase, phase === 'idle' ? 0 : tick, reduced).map((line, i) => (
        <Text color={color} key={`pet${i}`}>
          {line}
        </Text>
      ))}
      {quipRows.map((line, i) => (
        <Text color={color} key={`quip${i}`}>
          {line}
        </Text>
      ))}
    </Box>
  )
}
