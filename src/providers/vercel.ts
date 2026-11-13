import { setTimeout as sleep } from "node:timers/promises";
import { APIError, Sandbox } from "@vercel/sandbox";
import type { Provider } from "./types.ts";

/** How long to wait for Vercel Container Registry to prepare an image after a push. */
const IMAGE_READY_TIMEOUT_MS = 15 * 60 * 1000;
const IMAGE_READY_POLL_MS = 15 * 1000;

/**
 * Reads an access token and its scope from the environment.
 *
 * VERCEL_TEAM is the team slug or the team ID. VERCEL_PROJECT is the project name or
 * the project ID. The Vercel API accepts both forms.
 *
 * With all three variables set, the SDK uses them. With none set, the SDK falls back
 * to its default, the VERCEL_OIDC_TOKEN that `vercel env pull` writes.
 */
function credentialsFromEnv(): { token: string; teamId: string; projectId: string } | undefined {
  const { VERCEL_TOKEN, VERCEL_TEAM, VERCEL_PROJECT } = process.env;
  const present = [VERCEL_TOKEN, VERCEL_TEAM, VERCEL_PROJECT].filter(Boolean).length;
  if (present === 0) return undefined;
  if (!VERCEL_TOKEN || !VERCEL_TEAM || !VERCEL_PROJECT) {
    throw new Error("Set all of VERCEL_TOKEN, VERCEL_TEAM and VERCEL_PROJECT, or none of them.");
  }
  return { token: VERCEL_TOKEN, teamId: VERCEL_TEAM, projectId: VERCEL_PROJECT };
}

function isImageNotReady(error: unknown): boolean {
  if (!(error instanceof APIError)) return false;
  const json = error.json as { error?: { code?: string } } | undefined;
  return json?.error?.code === "image_not_ready";
}

/**
 * Starts sandboxes on Vercel Sandbox from an image in Vercel Container Registry (VCR).
 *
 * Vercel Sandbox pulls images only from VCR. After a push, VCR needs some time to
 * prepare the image, and sandbox creation fails with `image_not_ready` until then.
 */
export const vercel: Provider = {
  name: "vercel",

  async create(image, { timeoutMs, log }) {
    const credentials = credentialsFromEnv();
    const deadline = Date.now() + IMAGE_READY_TIMEOUT_MS;

    let sandbox: Sandbox;
    for (;;) {
      try {
        // `persistent: false` keeps no filesystem snapshot when the sandbox stops.
        sandbox = await Sandbox.create({ image, timeout: timeoutMs, persistent: false, ...credentials });
        break;
      } catch (error) {
        if (!isImageNotReady(error) || Date.now() + IMAGE_READY_POLL_MS > deadline) throw error;
        log?.(`VCR is still preparing the image. Next try in ${IMAGE_READY_POLL_MS / 1000} s.`);
        await sleep(IMAGE_READY_POLL_MS);
      }
    }

    return {
      id: sandbox.name,

      async exec(argv) {
        const [command, ...args] = argv;
        if (command === undefined) throw new Error("exec needs a command.");
        const result = await sandbox.runCommand(command, args);
        const [stdout, stderr] = await Promise.all([result.stdout(), result.stderr()]);
        return { exitCode: result.exitCode, stdout, stderr };
      },

      async destroy() {
        try {
          await sandbox.stop();
        } finally {
          await sandbox.delete({ deleteOrphanSnapshots: true });
        }
      },
    };
  },
};
