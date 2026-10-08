import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const MAX = 1000

const home = () => process.env.K3CODE_HOME ?? join(homedir(), '.k3code')

// Per-project: each cwd gets its own history file, so ↑ recalls prompts
// relevant to the repo the user is actually in.
const dir = (projectKey: string) => join(home(), 'projects', projectKey)
const file = (projectKey: string) => join(dir(projectKey), '.input_history')

/** Stable per-project key: the cwd with path separators flattened. */
export const projectHistoryKey = (projectRoot?: string): string => {
  const root = projectRoot ?? process.env.K3CODE_PROJECT_CWD ?? process.cwd()

  return root.replace(/[/:\\]+/g, '_') || 'default'
}

const caches = new Map<string, string[]>()

export function load(projectKey: string = projectHistoryKey()) {
  const cached = caches.get(projectKey)

  if (cached) {
    return cached
  }

  const f = file(projectKey)
  let entries: string[] = []

  try {
    if (existsSync(f)) {
      let current: string[] = []

      for (const line of readFileSync(f, 'utf8').split('\n')) {
        if (line.startsWith('+')) {
          current.push(line.slice(1))
        } else if (current.length) {
          entries.push(current.join('\n'))
          current = []
        }
      }

      if (current.length) {
        entries.push(current.join('\n'))
      }

      entries = entries.slice(-MAX)
    }
  } catch {
    entries = []
  }

  caches.set(projectKey, entries)

  return entries
}

export function append(line: string, projectKey: string = projectHistoryKey()) {
  const trimmed = line.trim()

  if (!trimmed) {
    return
  }

  const items = load(projectKey)

  if (items.at(-1) === trimmed) {
    return
  }

  items.push(trimmed)

  if (items.length > MAX) {
    items.splice(0, items.length - MAX)
  }

  try {
    mkdirSync(dir(projectKey), { recursive: true })

    const ts = new Date().toISOString().replace('T', ' ').replace('Z', '')

    const encoded = trimmed
      .split('\n')
      .map(l => `+${l}`)
      .join('\n')

    appendFileSync(file(projectKey), `\n# ${ts}\n${encoded}\n`)
  } catch {
    void 0
  }
}
