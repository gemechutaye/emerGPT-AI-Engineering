import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
const directory = await mkdtemp(join(tmpdir(), "emer-api-types-"));
try {
  const generated = join(directory, "api.ts");
  const result = spawnSync(
    process.execPath,
    [
      "node_modules/openapi-typescript/bin/cli.js",
      "openapi.json",
      "-o",
      generated,
    ],
    { stdio: "inherit" },
  );
  if (result.status !== 0) process.exitCode = result.status ?? 1;
  else if (
    (await readFile(generated, "utf8")) !==
    (await readFile("src/generated/api.ts", "utf8"))
  ) {
    process.stderr.write(
      "Generated API types are stale. Run npm run types:generate.\n",
    );
    process.exitCode = 1;
  } else
    process.stdout.write(
      "Generated API types match the checked-in OpenAPI document.\n",
    );
} finally {
  await rm(directory, { recursive: true, force: true });
}
