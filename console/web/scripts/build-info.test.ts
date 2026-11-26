import { execFileSync } from "node:child_process"
import { mkdtempSync, readFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"
// @ts-expect-error: a plain .mjs build script, no types
import { buildInfo } from "./build-info.mjs"

const SCRIPT = fileURLToPath(new URL("./build-info.mjs", import.meta.url))

function run(env: Record<string, string>): unknown {
  const out = join(mkdtempSync(join(tmpdir(), "build-info-")), "build.json")
  const keep = { PATH: process.env.PATH ?? "" }
  execFileSync(process.execPath, [SCRIPT, out], { env: { ...keep, ...env } })
  return JSON.parse(readFileSync(out, "utf8"))
}

describe("build-info.mjs (#182)", () => {
  it("writes the build arguments it is given", () => {
    expect(run({ VERSION: "0.103.0", REVISION: "28D6475A0C3E" }))
      .toEqual({ version: "0.103.0", revision: "28d6475a0c3e" })
  })

  it("writes null for a missing or unknown value, the Dockerfile's default", () => {
    expect(run({})).toEqual({ version: null, revision: null })
    expect(run({ VERSION: "unknown", REVISION: "unknown" })).toEqual({ version: null, revision: null })
    expect(buildInfo({ VERSION: " ", REVISION: "" })).toEqual({ version: null, revision: null })
  })
})
