import { createHash } from "node:crypto";
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";

const PRIVATE_REVISION = "71c9bf0caa69e07308c7df3937348073d7a3253e";
const utf8 = new TextDecoder("utf-8", { fatal: true });
const require = createRequire(import.meta.url);

function withPragma(source) {
  const lines = source.split("\n");
  const index = lines[0].startsWith("#!") ? 1 : 0;
  if (/^\/\/ @bun(?:\s|$)/.test(lines[index] ?? "")) {
    if (lines[index].includes("@bun-cjs"))
      throw new Error("CommonJS input requires its own bytecode emitter");
    if (!lines[index].includes("@bytecode")) lines[index] += " @bytecode";
  } else {
    lines.splice(index, 0, "// @bun @bytecode");
  }
  return lines.join("\n");
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

/** Emit each sidecar from the final source using the same private runtime that loads it. */
export function generateCaches(libraryPath, sourcePaths) {
  if (Bun.revision !== PRIVATE_REVISION || Bun.isStandaloneExecutable)
    throw new Error("cache generation requires the pinned clean private Bun runtime");
  const library = require(resolve(libraryPath));
  const metadata = {};
  library.validate();
  for (const sourcePath of sourcePaths) {
    const path = resolve(sourcePath);
    try {
      const source = Buffer.from(withPragma(utf8.decode(readFileSync(path))));
      const payload = library.emitModule(path, source);
      // Bun verifies this framing before JSC sees the unchecked cache payload.
      const footer = Buffer.alloc(24);
      footer.writeBigUInt64LE(BigInt(payload.length));
      footer.writeBigUInt64LE(Bun.hash.wyhash(payload), 8);
      footer.write("\0bun.jsc", 16);
      const framed = Buffer.concat([payload, footer]);
      writeFileSync(path, source);
      const temporary = `${path}.jsc.tmp-${process.pid}`;
      writeFileSync(temporary, framed);
      renameSync(temporary, `${path}.jsc`);
      metadata[path] = { sha256: sha256(framed), size: framed.length, source_sha256: sha256(source) };
    } catch (error) {
      throw new Error(`${path}: ${error instanceof Error ? error.message : String(error)}`, { cause: error });
    }
  }
  return metadata;
}

if (import.meta.main) {
  try {
    const [library, jobsPath, ...extra] = process.argv.slice(2);
    if (!library || !jobsPath || extra.length)
      throw new Error("usage: bun cache.mjs LIBWIRE_NODE JOBS_JSON");
    const paths = JSON.parse(readFileSync(jobsPath, "utf8"));
    if (!Array.isArray(paths) || !paths.length || paths.some((path) => typeof path !== "string"))
      throw new Error("cache jobs must be a nonempty array of source paths");
    process.stdout.write(`${JSON.stringify(generateCaches(library, paths))}\n`);
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
