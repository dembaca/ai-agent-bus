// Test helper: a source file's text, read from disk. The CSS tests read the
// stylesheets as text (vitest turns an imported .css into an empty module);
// @types/node is not a dependency; node-shim.d.ts declares the calls.
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"

/** The text of a file under src/, by its path relative to src/. */
export function sourceText(relative: string): string {
  return readFileSync(fileURLToPath(new URL(`./${relative}`, import.meta.url)), "utf8")
}
