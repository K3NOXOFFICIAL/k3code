import { atom } from 'nanostores'

export type ProposalKind = 'also_setup' | 'consequence' | 'improvement'

export interface Proposal {
  action: string
  id: string
  kind: ProposalKind
  text: string
}

export const PROPOSALS_MAX = 3

/** Pending proposals from `proposal.show` events, oldest first (cards render in this order). */
export const $proposals = atom<Proposal[]>([])

export const addProposal = (p: Proposal) => {
  const cur = $proposals.get()

  if (cur.some(c => c.id === p.id)) {
    return
  }

  $proposals.set([...cur, p].slice(-PROPOSALS_MAX))
}

export const removeProposal = (id: string) => $proposals.set($proposals.get().filter(p => p.id !== id))

export interface ProposalHandlers {
  accept: (p: Proposal) => void
  dismiss: (p: Proposal) => void
}

let handlers: ProposalHandlers | null = null

export const setProposalHandlers = (h: ProposalHandlers | null) => {
  handlers = h
}

/** `a` accepts and `d` dismisses the top card. Returns true when the key was consumed. */
export function handleProposalKey(ch: string, inputEmpty: boolean): boolean {
  const top = $proposals.get()[0]

  if (!top || !inputEmpty || (ch !== 'a' && ch !== 'd')) {
    return false
  }

  removeProposal(top.id)
  ch === 'a' ? handlers?.accept(top) : handlers?.dismiss(top)

  return true
}

export const GLYPH: Record<ProposalKind, string> = { also_setup: '＋', consequence: '⚠', improvement: '↑' }
