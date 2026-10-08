import shared from "../eslint.config.shared.mjs";

export default [
  ...shared,
  {
    // Base no-redeclare is wrong for these files: it flags TypeScript overloads
    // (geometry.ts) and a const and a type that share one name, which TS allows
    // (node.ts, enums.ts). Keep it off for these files only.
    files: [
      "packages/hermes-ink/src/ink/layout/geometry.ts",
      "packages/hermes-ink/src/ink/layout/node.ts",
      "packages/hermes-ink/src/native-ts/yoga-layout/enums.ts",
    ],
    rules: {
      "no-redeclare": "off",
    },
  },
  {
    // React Compiler output: the effect callback and its deps are cached in
    // variables (`useEffect(t2, t3)`), which react-hooks/exhaustive-deps cannot
    // analyse. The deps were checked by hand; keep the rule off for these files only.
    files: [
      "packages/hermes-ink/src/ink/components/Button.tsx",
      "packages/hermes-ink/src/ink/components/ClockContext.tsx",
    ],
    rules: {
      "react-hooks/exhaustive-deps": "off",
    },
  },
];
