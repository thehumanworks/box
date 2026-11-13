/**
 * Smoke test of the sandbox image.
 *
 * Starts one sandbox from the image on one provider, runs a fixed list of checks,
 * and destroys the sandbox. The exit code is 0 only if all checks pass.
 *
 *   node src/smoke.ts --provider modal  --image ghcr.io/<owner>/<repo>/base@sha256:<digest>
 *   node src/smoke.ts --provider vercel --image vcr.vercel.com/<team>/<project>/base@sha256:<digest>
 */
import { parseArgs } from "node:util";
import { modal } from "./providers/modal.ts";
import type { ExecResult, Provider, SandboxHandle } from "./providers/types.ts";
import { vercel } from "./providers/vercel.ts";

const PROVIDERS: Record<string, Provider> = { modal, vercel };

/** The sandbox needs a short life only. The provider stops it if this process dies. */
const SANDBOX_TIMEOUT_MS = 10 * 60 * 1000;

/** The record of installed agents that the home writes (home/profiles/agents.nix). */
const MANIFEST_PATH = "/root/.config/box/manifest.json";

/** The global instruction file of each agent (home/profiles/agents.nix). */
const INSTRUCTION_FILES = ["/root/.claude/CLAUDE.md", "/root/.codex/AGENTS.md", "/root/.pi/agent/AGENTS.md"];

interface Manifest {
  agents: { command: string; version: string }[];
}

interface Check {
  name: string;
  argv: string[];
  /** Returns a failure reason, or undefined if the output is correct. */
  verify?: (result: ExecResult) => string | undefined;
}

function expectOutput(expected: string) {
  return ({ stdout }: ExecResult) =>
    stdout.includes(expected) ? undefined : `the output does not contain "${expected}"`;
}

/** Reads the manifest from the sandbox. The image is wrong if it is absent or invalid. */
async function readManifest(sandbox: SandboxHandle): Promise<Manifest> {
  const result = await sandbox.exec(["cat", MANIFEST_PATH]);
  if (result.exitCode !== 0) {
    throw new Error(`The image has no manifest at ${MANIFEST_PATH}: ${result.stderr.trim()}`);
  }
  const manifest = JSON.parse(result.stdout) as Manifest;
  if (!Array.isArray(manifest.agents) || manifest.agents.length === 0) {
    throw new Error(`The manifest at ${MANIFEST_PATH} lists no agents.`);
  }
  return manifest;
}

function buildChecks(manifest: Manifest): Check[] {
  const agentChecks = manifest.agents.map(({ command, version }) => ({
    name: `${command} is version ${version}`,
    argv: [command, "--version"],
    verify: expectOutput(version),
  }));

  return [
    ...agentChecks,
    { name: "git runs", argv: ["git", "--version"] },
    { name: "mise runs", argv: ["mise", "--version"] },
    { name: "nix runs", argv: ["nix", "--version"] },
    {
      name: "nix reaches the public binary cache",
      argv: ["nix", "store", "info", "--store", "https://cache.nixos.org"],
    },
    {
      name: "each agent has the shared instructions",
      argv: ["sh", "-c", INSTRUCTION_FILES.map((file) => `test -s ${file}`).join(" && ")],
    },
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
      name: "the image ENV is applied (PATH has the Nix profile)",
      argv: ["printenv", "PATH"],
      verify: expectOutput("/root/.nix-profile/bin"),
    },
  ];
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

  console.log(`provider: ${provider.name}`);
  console.log(`image:    ${values.image}`);

  const started = Date.now();
  const sandbox = await provider.create(values.image, { timeoutMs: SANDBOX_TIMEOUT_MS, log: console.log });
  console.log(`sandbox:  ${sandbox.id} (ready after ${((Date.now() - started) / 1000).toFixed(1)} s)\n`);

  let failures: number;
  let total: number;
  try {
    const checks = buildChecks(await readManifest(sandbox));
    total = checks.length;
    failures = await runChecks(sandbox, checks);
  } finally {
    await sandbox.destroy();
  }

  console.log(`\n${total - failures} of ${total} checks passed on ${provider.name}.`);
  return failures === 0 ? 0 : 1;
}

process.exitCode = await main();
