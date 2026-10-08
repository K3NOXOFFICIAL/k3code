// Shared ESLint flat config for the k3code repo, imported by tui/eslint.config.mjs.
// This file sits at the repo root, which has no node_modules, so every package
// is resolved from tui/, the package that installs the lint toolchain.
import { createRequire } from 'node:module'

const tuiRequire = createRequire(new URL('./tui/package.json', import.meta.url))
const js = tuiRequire('@eslint/js')
const tseslint = tuiRequire('typescript-eslint')
const reactHooks = tuiRequire('eslint-plugin-react-hooks')

const SOURCE_FILES = ['**/*.{js,mjs,cjs,ts,tsx,mts,cts}']

// Recommended rules that fire on the existing code. They are switched off, not
// fixed, so the lint stays green. The number is the finding count when this
// config was written (388 errors in 59 of 480 files). Turn one back on once its
// findings are fixed.
const EXISTING_FINDINGS_OFF = {
  '@typescript-eslint/no-explicit-any': 'off', // 306
  '@typescript-eslint/no-unused-vars': 'off', // 43
  '@typescript-eslint/no-unused-expressions': 'off', // 13
  'no-fallthrough': 'off', // 12
  'no-useless-assignment': 'off', // 7
  'react-hooks/exhaustive-deps': 'off', // 3
  '@typescript-eslint/triple-slash-reference': 'off', // 1
  '@typescript-eslint/no-require-imports': 'off', // 1
  'no-var': 'off', // 1
  'prefer-const': 'off' // 1
}

export default [
  { ignores: ['**/dist/**', '**/node_modules/**'] },
  { ...js.configs.recommended, files: SOURCE_FILES },
  ...tseslint.configs.recommended.map(config => ({ files: SOURCE_FILES, ...config })),
  {
    files: SOURCE_FILES,
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      ...EXISTING_FINDINGS_OFF
    }
  }
]
