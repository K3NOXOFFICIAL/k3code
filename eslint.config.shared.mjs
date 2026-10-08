// Shared ESLint flat config for the k3code repo, imported by tui/eslint.config.mjs.
// This file sits at the repo root, which has no node_modules, so every package
// is resolved from tui/, the package that installs the lint toolchain.
import { createRequire } from 'node:module'

const tuiRequire = createRequire(new URL('./tui/package.json', import.meta.url))
const js = tuiRequire('@eslint/js')
const tseslint = tuiRequire('typescript-eslint')
const reactHooks = tuiRequire('eslint-plugin-react-hooks')

const SOURCE_FILES = ['**/*.{js,mjs,cjs,ts,tsx,mts,cts}']

// Recommended rules kept switched off by policy. Only no-explicit-any is left.
const POLICY_OFF = {
  '@typescript-eslint/no-explicit-any': 'off' // Deliberate: gateway protocol carries untyped JSON; typing the ~306 uses is churn risk.
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
      'react-hooks/exhaustive-deps': 'error',
      'no-redeclare': 'error',
      '@typescript-eslint/consistent-type-imports': 'error',
      // Parameters named _x are intentionally unused where their position is fixed.
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', ignoreRestSiblings: true }],
      ...POLICY_OFF
    }
  }
]
