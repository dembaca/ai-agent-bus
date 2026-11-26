// The console's build identity (#182), written at image build:
//   VERSION=<x.y.z> REVISION=<commit> node build-info.mjs <out.json>
// The Dockerfile passes its build arguments; "unknown" or empty (a build
// without them) is written as null, which the footer shows as n/a and the
// metric and the OCI labels as "unknown" — the one fallback of all three.
import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

/** A given value, or null for none ("", "unknown"). */
export function given(value) {
  const v = (value ?? "").trim();
  return v === "" || v === "unknown" ? null : v;
}

/** The file's content for the given environment. */
export function buildInfo(env) {
  const revision = given(env.REVISION);
  return { version: given(env.VERSION), revision: revision && revision.toLowerCase() };
}

const outFile = process.argv[2];
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (!outFile) {
    console.error("usage: VERSION=… REVISION=… node build-info.mjs <out.json>");
    process.exit(2);
  }
  writeFileSync(outFile, JSON.stringify(buildInfo(process.env)) + "\n");
}
