// The few Node calls the CSS tests use (src/sourceText.ts); @types/node is not a dependency.
declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string
  export function readdirSync(path: string): string[]
}
declare module "node:url" {
  export function fileURLToPath(url: URL): string
}
