import { ModalClient } from "modal";
import type { Provider } from "./types.ts";

/** Modal groups sandboxes under an App. All sandboxes of this project use this one. */
const APP_NAME = process.env.BOX_MODAL_APP ?? "box";

/**
 * Starts sandboxes on Modal from a registry image.
 *
 * Authentication comes from the Modal SDK: the MODAL_TOKEN_ID and MODAL_TOKEN_SECRET
 * environment variables, or the active profile in ~/.modal.toml.
 */
export const modal: Provider = {
  name: "modal",

  async create(image, { timeoutMs }) {
    const client = new ModalClient();
    try {
      const app = await client.apps.fromName(APP_NAME, { createIfMissing: true });
      // An ID such as "im-abc123" names an image that Modal built itself. This permits a
      // test of image/Dockerfile with Modal's builder, without a registry.
      const modalImage = image.startsWith("im-")
        ? await client.images.fromId(image)
        : client.images.fromRegistry(image);
      const sandbox = await client.sandboxes.create(app, modalImage, { timeoutMs });

      return {
        id: sandbox.sandboxId,

        async exec(argv) {
          const process = await sandbox.exec(argv);
          const [stdout, stderr, exitCode] = await Promise.all([
            process.stdout.readText(),
            process.stderr.readText(),
            process.wait(),
          ]);
          return { exitCode, stdout, stderr };
        },

        async destroy() {
          try {
            await sandbox.terminate();
          } finally {
            client.close();
          }
        },
      };
    } catch (error) {
      client.close();
      throw error;
    }
  },
};
