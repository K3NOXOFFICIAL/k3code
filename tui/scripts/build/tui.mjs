#!/usr/bin/env node
// Self-contained TUI compiler. Dependencies must already be prepared.
import {
  cpSync,
  readFileSync,
  writeFileSync,
  mkdtempSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { pathToFileURL } from "node:url";
import {
  frontendArgs,
  isMain,
  productOutput,
  publishDirectory,
  repoRoot,
  withProduct,
  workspaceTool,
} from "./frontend-common.mjs";
import { recordProduct, buildInputs } from "./freshness.mjs";

// `react-devtools-core` is only imported when DEV=true at runtime (Ink dev
// mode). Stub it out so the bundle doesn't carry the dep.
const stubDevtools = {
  name: "stub-react-devtools-core",
  setup(b) {
    b.onResolve({ filter: /^react-devtools-core$/ }, (args) => ({
      path: args.path,
      namespace: "stub-devtools",
    }));
    b.onLoad({ filter: /.*/, namespace: "stub-devtools" }, () => ({
      contents: "export default { initialize() {}, connectToDevTools() {} }",
      loader: "js",
    }));
  },
};

export async function buildTui(options) {
  const { source, out: destination } = productOutput(
    options.source,
    options.out,
    ["packages", "shared", "src", "node_modules"],
  );
  const { build } = await import(
    pathToFileURL(workspaceTool(source, ".", "esbuild")).href
  );
  const root = source;
  const inputs = buildInputs(source, "tui");
  await withProduct(destination, async (product) => {
    const out = join(product, "dist/entry.js");
    await build({
      absWorkingDir: root,
      entryPoints: [resolve(root, "src/entry.tsx")],
      bundle: true,
      platform: "node",
      format: "esm",
      target: "node20",
      outfile: out,
      jsx: "automatic",
      jsxImportSource: "react",
      // Skip the prebuilt @hermes/ink bundle and inline the source instead:
      // (1) esbuild's `__esm` helper does not await nested async init, so the
      //     prebuilt bundle's lazy `render` would never resolve when nested in
      //     this top-level Promise.all; (2) bundling from source also lets us
      //     keep `ink-text-input` and the upstream `ink` graph OUT of the
      //     bundle entirely — re-exporting them from entry-exports created a
      //     circular async chain that hung the TUI at startup with only ANSI
      //     reset bytes on screen (#31227).
      alias: {
        "@k3code/ink": resolve(
          root,
          "packages/hermes-ink/src/entry-exports.ts",
        ),
      },
      // Bake React's production build into dist/ (react-reconciler's dev build alone is ~1.1 MB
      // of source). NODE_ENV is fixed at build time, so the bundle never switches to dev mode.
      define: { "process.env.NODE_ENV": '"production"' },
      // keepNames: function/class names stay readable in stack traces written to the logs.
      minify: true,
      keepNames: true,
      plugins: [stubDevtools],
      // Some transitive deps use CommonJS `require(...)` at runtime. ESM bundles
      // don't get a `require` binding automatically, so we inject one.
      banner: {
        js: "import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);",
      },
      logLevel: "info",
    });

    // esbuild preserves the shebang from src/entry.tsx into the bundle, but Nix's
    // patchShebangs phase mangles `/usr/bin/env -S node --foo --bar` (it strips
    // the `node` token, leaving a broken interpreter). The hermes_cli launcher
    // always invokes this file as `node dist/entry.js` anyway, so the shebang is
    // redundant — strip it.
    const body = readFileSync(out, "utf8");
    if (body.startsWith("#!")) {
      writeFileSync(out, body.slice(body.indexOf("\n") + 1));
    }
    writeFileSync(
      join(product, "package.json"),
      JSON.stringify({ type: "module" }) + "\n",
    );
    recordProduct({
      source,
      product: "tui",
      out: join(product, "dist"),
      inputs,
    });
  });
  return { out: destination, entry: join(destination, "dist/entry.js") };
}

// npm's existing developer entrypoint retains ui-tui/dist without replacing
// the source workspace (a product includes its own package.json).
async function buildDeveloperTui() {
  const scratch = mkdtempSync(join(tmpdir(), "k3code-tui-"));
  try {
    const result = await buildTui({
      source: repoRoot,
      out: join(scratch, "tui"),
    });
    // TMPDIR and the source checkout need not share a filesystem.
    const staged = mkdtempSync(join(repoRoot, ".dist-"));
    try {
      cpSync(join(result.out, "dist"), staged, { recursive: true });
      publishDirectory(staged, join(repoRoot, "dist"), { source: repoRoot });
    } finally {
      rmSync(staged, { recursive: true, force: true });
    }
    return { out: join(repoRoot), entry: join(repoRoot, "dist/entry.js") };
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}

if (isMain(import.meta.url)) {
  try {
    const result =
      process.argv.length === 2
        ? await buildDeveloperTui()
        : await buildTui(frontendArgs(process.argv.slice(2)));
    console.log(`built ${result.entry}`);
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  }
}
