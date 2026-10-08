import { atom } from 'nanostores'

export type ProposalKind =
  | 'also_setup'
  | 'consequence'
  | 'improvement'
  | 'optimizer'
  | 'permission_rule'
  | 'preference'
  | 'project_setup'
  | 'skill'

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

/**
 * Alt+Y accepts ("yes") and Alt+N dismisses ("no") the top card. Returns true when the key was consumed.
 *
 * A bare `a`/`d` on an empty composer used to do this, which hijacked the first letter of every message typed while
 * a card was showing ("add a test ..." accepted the card, "do ..." dismissed it for good). A modifier is unambiguous;
 * Alt+D is taken (the text input's readline kill-word), Alt+Y / Alt+N are not.
 */
export function handleProposalKey(ch: string, meta: boolean): boolean {
  const top = $proposals.get()[0]

  if (!top || !meta || (ch !== 'y' && ch !== 'n')) {
    return false
  }

  removeProposal(top.id)
  if (ch === 'y') {
    handlers?.accept(top)
  } else {
    handlers?.dismiss(top)
  }

  return true
}

/** Cards belong to the session that produced them: drop them when the active session changes. */
export const clearProposals = () => $proposals.set([])

export const GLYPH: Record<ProposalKind, string> = {
  also_setup: '＋',
  consequence: '⚠',
  improvement: '↑',
  optimizer: '⚙',
  permission_rule: '🔑',
  preference: '♥',
  project_setup: '📁',
  skill: '✦'
}

/** Fallback for kinds a newer gateway may send. */
export const glyphFor = (kind: string): string => (GLYPH as Record<string, string>)[kind] ?? '•'
