# Box engineering notes

For the Modal exec service, read [docs/remote-exec.md](docs/remote-exec.md). It records
the documentation sources, API contract, replay/timeout limits and verified slices.

- Deploy only when authorised: `scripts/deploy_remote.py` configures local TLS,
  deploys the app, and provisions/reuses a private proxy credential file.
- `scripts/smoke_remote.py` exercises the real deployed endpoints against an
  existing box. `scripts/smoke_cli_remote.py` tests the packaged CLI with an isolated
  real sandbox and cleans it up. Unit tests alone do not prove deployment/authentication.
- `exec`, `spawn` and `poll` use `remote_client.py`, not local Service/SDK calls.
  Select the HTTPS deployment explicitly or auto-discover exactly one local manifest.
  Never add automatic POST retries or a silent SDK fallback after HTTP failure.
- Keep the ASGI app proxy-authenticated. Do not upload `.env` or local API/proxy
  credentials into the image. Inject local deployment credentials with dotenvx.
- `retries=0` does not prevent infrastructure replays. Preserve atomic execution
  claims and stable input IDs; never release an uncertain claim and rerun blindly.
- Modal 1.6.3 `ContainerProcess.wait()` can return `-1` on deadline expiry instead
  of raising `ExecTimeoutError`. Do not report that sentinel as an ordinary exit.
- Exec never creates directories or repairs a box. It uses the existing readiness
  checks and Modal-native `workdir`; lifecycle operations remain separate.

Local checks: `uv sync --extra remote`, `uv run pytest -q`, and
`uv run ruff check src tests scripts/deploy_remote.py scripts/smoke_remote.py scripts/smoke_cli_remote.py`.
