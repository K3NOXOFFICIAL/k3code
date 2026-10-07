import { coreCommands } from './commands/core.js'
import { debugCommands } from './commands/debug.js'
import { opsCommands } from './commands/ops.js'
import { sessionCommands } from './commands/session.js'
import { setupCommands } from './commands/setup.js'
import type { SlashCommand } from './types.js'

// k3code M1 cut: subscription/topup (billing) and wake (wake-word) slash
// commands are removed — those surfaces and their gateway methods
// (subscription.*, billing.*, wake.*) are out of scope for k3code. See
// docs/tui-contract.md.
export const SLASH_COMMANDS: SlashCommand[] = [
  ...coreCommands,
  ...sessionCommands,
  ...opsCommands,
  ...setupCommands,
  ...debugCommands
]

const byName = new Map<string, SlashCommand>(
  SLASH_COMMANDS.flatMap(cmd => [cmd.name, ...(cmd.aliases ?? [])].map(name => [name, cmd] as const))
)

export const findSlashCommand = (name: string) => byName.get(name.toLowerCase())
