/** The result of one command that ran in a sandbox. */
export interface ExecResult {
  exitCode: number;
  stdout: string;
  stderr: string;
}

/** A running sandbox. */
export interface SandboxHandle {
  /** The identifier that the provider gave to the sandbox. */
  id: string;
  /** Runs one command (no shell) and waits until it ends. */
  exec(argv: string[]): Promise<ExecResult>;
  /** Stops the sandbox and removes it from the provider. */
  destroy(): Promise<void>;
}

export interface CreateOptions {
  /** The maximum lifetime of the sandbox, in milliseconds. */
  timeoutMs: number;
  /** Receives progress messages, for example while the provider prepares the image. */
  log?: (message: string) => void;
}

/** A hosting provider that can start a sandbox from an OCI image reference. */
export interface Provider {
  name: string;
  create(image: string, options: CreateOptions): Promise<SandboxHandle>;
}
