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

/** How to ask a tool of image/mise.toml for its version. */
interface Probe {
  /** The program that the tool puts on PATH. */
  bin: string;
  /** The default is `--version`. */
  args?: string[];
  /** Changes the version of the lockfile into the text that the tool prints. */
  printed?: (version: string) => string;
  /** Set to false if the tool cannot print its version. The check is then only that it runs. */
  printsVersion?: false;
}

/** One entry for each tool in image/mise.toml. The key is the name of the tool there. */
const PROBES: Record<string, Probe> = {
  node: { bin: "node" },
  bun: { bin: "bun" },
  pnpm: { bin: "pnpm" },
  python: { bin: "python" },
  uv: { bin: "uv" },
  rust: { bin: "rustc" },
  // The lockfile has the release of relocatable-perl ("5.44.0.0"). Perl prints "v5.44.0".
  perl: { bin: "perl", printed: (version) => `v${version.split(".").slice(0, 3).join(".")}` },
  ruby: { bin: "ruby" },
  claude: { bin: "claude" },
  codex: { bin: "codex" },
  pi: { bin: "pi" },
  doppler: { bin: "doppler" },
  fnox: { bin: "fnox" },
  hunk: { bin: "hunk" },
  gh: { bin: "gh" },
  helix: { bin: "hx" },
  ripgrep: { bin: "rg" },
  glow: { bin: "glow" },
  fzf: { bin: "fzf" },
  "github:thehumanworks/capnshell": { bin: "cns" },
  "github:thehumanworks/wit": { bin: "wit", args: ["--help"], printsVersion: false },
};

interface PinnedTool {
  name: string;
  version: string;
  probe: Probe;
}

/**
 * Reads the tools from the config that the image installs, and the version of each
 * from the lockfile. The config follows "latest", so only the lockfile has the versions.
 */
function pinnedTools(): PinnedTool[] {
  const read = (name: string) => parseToml(readFileSync(new URL(`../image/${name}`, import.meta.url), "utf8"));
  const config = read("mise.toml") as { tools?: Record<string, unknown> };
  const lock = read("mise.lock") as { tools?: Record<string, { version?: string }[]> };

  return Object.keys(config.tools ?? {}).map((name) => {
    const versions = (lock.tools?.[name] ?? []).map((entry) => entry.version);
    const [version] = versions;
    if (versions.length !== 1 || !version) {
      throw new Error(`image/mise.lock must have one version of ${name}. Run \`mise run image:lock\`.`);
    }
    const probe = PROBES[name];
    if (!probe) throw new Error(`src/smoke.ts has no entry in PROBES for the tool ${name}.`);
    return { name, version, probe };
  });
}

function expectOutput(expected: string) {
  return ({ stdout }: ExecResult) =>
    stdout.includes(expected) ? undefined : `the output does not contain "${expected}"`;
}

function buildChecks(tools: PinnedTool[]): Check[] {
  const checks: Check[] = [
    { name: "mise runs", argv: ["mise", "--version"] },
    { name: "git runs", argv: ["git", "--version"] },
    { name: "the C compiler runs", argv: ["cc", "--version"] },
  ];

  for (const { name, version, probe } of tools) {
    const argv = [probe.bin, ...(probe.args ?? ["--version"])];
    const verify = probe.printsVersion === false ? undefined : expectOutput(probe.printed?.(version) ?? version);
    checks.push({
      name: probe.printsVersion === false ? `${name} runs` : `${name} is version ${version}`,
      argv,
      verify,
    });
    // The image contract: a tool works even when the provider drops the image ENV.
    checks.push({
      name: `${name} runs without the image ENV`,
      argv: ["env", "-i", `PATH=${DEFAULT_PATH}`, ...argv],
      verify,
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
    {
      name: "Claude Code starts without the onboarding (~/.claude.json)",
      argv: ["jq", "-e", ".hasCompletedOnboarding", "/root/.claude.json"],
    },
    {
      // Python is compiled in the build. A missing library there drops a module silently.
      name: "python has its optional standard modules",
      argv: ["python", "-c", "import bz2, ctypes, curses, lzma, readline, sqlite3, ssl, uuid, zlib"],
    },
    {
      name: "fnox reads its default config",
      argv: ["fnox", "config-files"],
      verify: expectOutput("/root/.config/fnox/config.toml"),
    },
    {
      // The tools of mise must be before /usr/bin, where Ubuntu has its own perl.
      name: "an interactive shell has the mise tools and the mise environment",
      argv: ["bash", "-ic", 'command -v perl && echo "IS_SANDBOX=$IS_SANDBOX"'],
      verify: ({ stdout }) =>
        stdout.includes("/mise/") && stdout.includes("IS_SANDBOX=1")
          ? undefined
          : "perl is not from mise, or IS_SANDBOX is not 1",
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
