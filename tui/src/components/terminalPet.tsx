import { Box, Text } from '@k3code/ink'
import { useStore } from '@nanostores/react'
import { useEffect, useReducer, useRef, useState } from 'react'

import { $overlayState } from '../app/overlayStore.js'
import { $petEnabled, $petName } from '../app/petStore.js'
import type { PetName } from '../content/pets.js'
import { isReducedMotion } from '../lib/animation.js'
import {
  nextPetPhase,
  PET_COLUMN_WIDTH,
  PET_DONE_HOLD_MS,
  PET_IDLE_BLINK_EVERY_MS,
  PET_IDLE_BLINK_MS,
  PET_TICK_MS,
  type PetPhase,
  petColumnWidth,
  petLines
} from '../lib/terminalPet.js'
import type { Theme } from '../theme.js'

/** Every pet's idle frames end with its blink pose. */
const IDLE_BLINK_FRAME = -1

/** Corner column for the pet. Hidden when `/pet off` or the terminal is narrow. */
export function PetCorner({ busy, cols, t }: { busy: boolean; cols: number; t: Theme }) {
  const enabled = useStore($petEnabled)
  const name = useStore($petName)
  const overlay = useStore($overlayState)

  if (petColumnWidth(enabled, cols) === 0) {
    return null
  }

  const needsInput = Boolean(overlay.approval || overlay.clarify || overlay.confirm || overlay.secret || overlay.sudo)

  return (
    <Box flexDirection="column" flexShrink={0} justifyContent="flex-end" paddingLeft={1} width={PET_COLUMN_WIDTH}>
      <TerminalPet busy={busy} name={name} needsInput={needsInput} t={t} />
    </Box>
  )
}

/** Animated pet. Its own timer runs only while mounted, so unmounting (pet off,
 *  narrow terminal, agents view) clears it. */
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
  const busyRef = useRef(busy)
  const needsRef = useRef(false)

  useEffect(() => {
    if (busy !== busyRef.current) {
      busyRef.current = busy
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

  // Animate only while the pet is doing something; idle only blinks (below).
  const animating = !reduced && phase !== 'idle'

  useEffect(() => {
    if (!animating) {
      return
    }

    const id = setInterval(() => setTick(n => n + 1), PET_TICK_MS)

    return () => clearInterval(id)
  }, [animating])

  // Idle is a still pose that blinks now and then: one slow timer, no per-frame repaints.
  const [blink, setBlink] = useState(false)
  const idleBlinks = !reduced && phase === 'idle'

  useEffect(() => {
    if (!idleBlinks) {
      return
    }

    let open: ReturnType<typeof setTimeout> | undefined

    const id = setInterval(() => {
      setBlink(true)
      open = setTimeout(() => setBlink(false), PET_IDLE_BLINK_MS)
    }, PET_IDLE_BLINK_EVERY_MS)

    return () => {
      clearInterval(id)

      if (open !== undefined) {
        clearTimeout(open)
      }

      setBlink(false)
    }
  }, [idleBlinks])

  const color = phase === 'needs_input' ? t.color.warn : phase === 'done' ? t.color.accent : t.color.muted

  return (
    <Box flexDirection="column">
      {petLines(name, phase, phase === 'idle' ? (blink ? IDLE_BLINK_FRAME : 0) : tick, reduced).map((line, i) => (
        <Text color={color} key={i}>
          {line}
        </Text>
      ))}
    </Box>
  )
}
