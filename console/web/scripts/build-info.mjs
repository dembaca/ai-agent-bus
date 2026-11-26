// The console's build identity (#182), written at image build:
//   node build-info.mjs <plugin.json> <out.json>
// version: env VERSION if set, else the "version" of the given plugin.json;
// revision: env REVISION (lower-cased) if set, else null.
import { readFileSync, writeFileSync } from "node:fs";

const [pluginFile, outFile] = process.argv.slice(2);
if (!pluginFile || !outFile) {
  console.error("usage: node build-info.mjs <plugin.json> <out.json>");
  process.exit(2);
}
const version = process.env.VERSION
  ? process.env.VERSION
  : JSON.parse(readFileSync(pluginFile, "utf8")).version;
const revision = process.env.REVISION ? process.env.REVISION.toLowerCase() : null;
writeFileSync(outFile, JSON.stringify({ version, revision }) + "\n");
