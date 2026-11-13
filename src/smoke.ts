/**
 * Smoke test of the base image.
 *
 * Starts one sandbox from the image on one provider, runs a fixed list of checks,
 * and destroys the sandbox. The exit code is 0 only if all checks pass.
 *
 *   node src/smoke.ts --provider modal  --image ghcr.io/<owner>/<repo>/base@sha256:<digest>
 *   node src/smoke.ts --provider vercel --image vcr.vercel.com/<team>/<project>/base@sha256:<digest>
 */
import { readFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { parse as parseToml } from "smol-toml";
import { modal } from "./providers/modal.ts";
import type { ExecResult, Provider, SandboxHandle } from "./providers/types.ts";
import { vercel } from "./providers/vercel.ts";

const PROVIDERS: Record<string, Provider> = { modal, vercel };

/** The sandbox needs a short life only. The provider stops it if this process dies. */
const SANDBOX_TIMEOUT_MS = 10 * 60 * 1000;

/** The PATH of a Linux process when nothing sets one. It does not contain the mise directories. */
const DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

interface Check {
  name: string;
  argv: string[];
  /** Returns a failure reason, or undefined if the output is correct. */
  verify?: (result: ExecResult) => string | undefined;
}

/** Reads the pinned tool versions from the config that the image installs. */
function pinnedTools(): Record<string, string> {
  const path = new URL("../image/mise.toml", import.meta.url);
  const config = parseToml(readFileSync(path, "utf8")) as { tools?: Record<string, unknown> };
  const tools: Record<string, string> = {};
  for (const [tool, version] of Object.entries(config.tools ?? {})) {
    if (typeof version !== "string" || !/^\d+\.\d+\.\d+$/.test(version)) {
      throw new Error(`image/mise.toml must pin ${tool} to an exact version. Found: ${JSON.stringify(version)}`);
    }
    tools[tool] = version;
  }
  return tools;
}

function expectOutput(expected: string) {
  return ({ stdout }: ExecResult) =>
    stdout.includes(expected) ? undefined : `the output does not contain "${expected}"`;
}

function buildChecks(tools: Record<string, string>): Check[] {
  const checks: Check[] = [
    { name: "mise runs", argv: ["mise", "--version"] },
    { name: "git runs", argv: ["git", "--version"] },
  ];

  for (const [tool, version] of Object.entries(tools)) {
    checks.push({
      name: `${tool} is version ${version}`,
      argv: [tool, "--version"],
      verify: expectOutput(version),
    });
    // The image contract: a tool works even when the provider drops the image ENV.
    checks.push({
      name: `${tool} runs without the image ENV`,
      argv: ["env", "-i", `PATH=${DEFAULT_PATH}`, tool, "--version"],
      verify: expectOutput(version),
    });
  }

  checks.push(
    {
      name: "the user is root",
      argv: ["id", "-u"],
      verify: ({ stdout }) => (stdout.trim() === "0" ? undefined : `the user id is ${stdout.trim()}`),
    },
    {
      name: "the working directory is the image WORKDIR",
      argv: ["pwd"],
      verify: expectOutput("/workspace"),
    },
    {
      name: "the image ENV is applied (PATH has the mise shims)",
      argv: ["printenv", "PATH"],
      verify: expectOutput("/usr/local/share/mise/shims"),
    },
  );
  return checks;
}

function indent(text: string): string {
  return text
    .trimEnd()
    .split("\n")
    .map((line) => `      ${line}`)
    .join("\n");
}

async function runChecks(sandbox: SandboxHandle, checks: Check[]): Promise<number> {
  let failures = 0;
  for (const check of checks) {
    let reason: string | undefined;
    let result: ExecResult | undefined;
    try {
      result = await sandbox.exec(check.argv);
      if (result.exitCode !== 0) reason = `exit code ${result.exitCode}`;
      else reason = check.verify?.(result);
    } catch (error) {
      // A provider can throw, in place of an exit code, when the program does not exist.
      reason = `the provider could not run the command: ${error instanceof Error ? error.message : String(error)}`;
    }

    console.log(`${reason ? "FAIL" : "ok  "}  ${check.name}`);
    if (reason) {
      failures += 1;
      console.log(`      $ ${check.argv.join(" ")}`);
      console.log(`      ${reason}`);
      if (result?.stdout.trim()) console.log(indent(`stdout: ${result.stdout}`));
      if (result?.stderr.trim()) console.log(indent(`stderr: ${result.stderr}`));
    }
  }
  return failures;
}

async function main(): Promise<number> {
  const { values } = parseArgs({
    options: { provider: { type: "string" }, image: { type: "string" } },
  });
  const provider = values.provider ? PROVIDERS[values.provider] : undefined;
  if (!provider || !values.image) {
    console.error(`Usage: node src/smoke.ts --provider <${Object.keys(PROVIDERS).join("|")}> --image <reference>`);
    return 2;
  }

  const checks = buildChecks(pinnedTools());
  console.log(`provider: ${provider.name}`);
  console.log(`image:    ${values.image}`);

  const started = Date.now();
  const sandbox = await provider.create(values.image, { timeoutMs: SANDBOX_TIMEOUT_MS, log: console.log });
  console.log(`sandbox:  ${sandbox.id} (ready after ${((Date.now() - started) / 1000).toFixed(1)} s)\n`);

  let failures: number;
  try {
    failures = await runChecks(sandbox, checks);
  } finally {
    await sandbox.destroy();
  }

  console.log(`\n${checks.length - failures} of ${checks.length} checks passed on ${provider.name}.`);
  return failures === 0 ? 0 : 1;
}

process.exitCode = await main();
