import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  $proposals,
  addProposal,
  glyphFor,
  handleProposalKey,
  PROPOSALS_MAX,
  removeProposal,
  setProposalHandlers
} from '../../k3/proposalsStore.js'

const card = (id: string) => ({ action: `do ${id}`, id, kind: 'improvement' as const, text: `text ${id}` })

afterEach(() => {
  $proposals.set([])
  setProposalHandlers(null)
})

describe('proposal cards', () => {
  it('dedupes by id and keeps the newest three', () => {
    ;['p1', 'p1', 'p2', 'p3', 'p4'].forEach(id => addProposal(card(id)))
    expect($proposals.get().map(p => p.id)).toEqual(['p2', 'p3', 'p4'])
    expect($proposals.get().length).toBeLessThanOrEqual(PROPOSALS_MAX)
  })

  it('a accepts and d dismisses the top card, only with an empty composer', () => {
    const accept = vi.fn()
    const dismiss = vi.fn()
    setProposalHandlers({ accept, dismiss })
    addProposal(card('p1'))
    addProposal(card('p2'))

    expect(handleProposalKey('a', false)).toBe(false) // typing a message: never hijacked
    expect(handleProposalKey('x', true)).toBe(false)
    expect(handleProposalKey('a', true)).toBe(true)
    expect(accept).toHaveBeenCalledWith(card('p1'))
    expect(handleProposalKey('d', true)).toBe(true)
    expect(dismiss).toHaveBeenCalledWith(card('p2'))
    expect(handleProposalKey('a', true)).toBe(false) // no cards left
  })

  it('removeProposal drops one card', () => {
    addProposal(card('p1'))
    removeProposal('p1')
    expect($proposals.get()).toEqual([])
  })
})

describe('kind icons', () => {
  it('has a distinct icon for every learned kind and a fallback for unknown ones', () => {
    const kinds = ['permission_rule', 'preference', 'project_setup', 'skill', 'optimizer', 'consequence', 'improvement']
    const icons = kinds.map(glyphFor)

    expect(new Set(icons).size).toBe(kinds.length)
    expect(glyphFor('something_new')).toBe('•')
  })
})
