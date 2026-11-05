# Remote exec on Modal

`box-exec-api` deploys one execution worker and an authenticated FastAPI service.
It runs commands in existing Box sandboxes; it does not create or repair them.
The local `box exec` CLI still uses its original direct connection.

## Deploy and verify

Run from the repository with an existing ready box and the encrypted `.env`:

```sh
uv sync --extra remote
dotenvx run --quiet --strict -f .env -- \
  uv run --extra remote python scripts/deploy_remote.py
```

The helper uses the active Modal workspace/environment; `--environment NAME`
selects another environment. The named box must exist in that environment's
`box-dev-registry-v1` registry. Redeployment updates `box-exec-api` in place.

The helper prints the deployment URL and paths to two local files:

- `deployment.json`: URL, app ID, workspace/environment and credential-file path.
- `proxy.json`: HTTP proxy credentials, stored outside the repository with mode
  `0600`, under `$XDG_CONFIG_HOME/box/box-exec-api/<workspace>/<environment>/`
  (default `~/.config/box/...`). Existing credentials are reused.

The API token from `.env` is used only in the local deployment process. The image
contains the locked dependencies and `box` Python source, not `.env`, API tokens
or proxy credentials. Hosted functions use Modal's runtime credentials.

Run the real HTTP checks with the printed manifest path:

```sh
uv run --extra remote python scripts/smoke_remote.py \
  --deployment ~/.config/box/box-exec-api/imagent/main/deployment.json \
  --name lf-box --output docs/remote-exec-verification.json
```

This verifies real commands, authentication on all routes, literal arguments,
working directories, stdout/stderr, nonzero exits, timeouts, background calls,
repeat polling, duplicate protection and output limits. It creates unique files
under `/tmp` in the named box and removes those test-owned files afterwards.
The evidence file contains no credentials or command output.

## HTTP contract

All routes require Modal's `Modal-Key` and `Modal-Secret` headers. These are proxy
tokens (`wk-`/`ws-`), **not** the API tokens (`ak-`/`as-`) used for deployment.
Missing and invalid authentication are rejected by Modal before FastAPI runs.

**Treat proxy-token holders as trusted shell operators.** This service can execute
commands in any managed box in its environment. Proxy tokens are workspace-wide
in non-RBAC workspaces, including this deployment; RBAC workspaces can scope them
to an environment, not to an individual endpoint. A dedicated token provides a
revocation handle, not endpoint-specific isolation.

### Request body for `/exec` and `/spawn`

```json
{
  "name": "lf-box",
  "command": ["nix", "--version"],
  "cwd": "/workspace",
  "timeout": 60,
  "request_id": "a-new-unique-id-for-this-operation"
}
```

| Field | Meaning |
| --- | --- |
| `name` | Existing Box name, using the same validation as the CLI. |
| `command` | Nonempty argv array; arguments stay literal. No shell expansion unless you explicitly invoke a shell. Maximum 256 arguments / 64 KiB combined UTF-8. |
| `cwd` | Optional existing absolute directory; defaults to the box's saved directory. No NUL or `..`. An override permits inspecting incomplete setup, as in the CLI. |
| `timeout` | Integer seconds, default 60. `/exec`: 1–90; `/spawn` and SDK: 1–3600. |
| `request_id` | Optional 8–128 ASCII letters, digits, underscores or hyphens. Supply a UUID-derived value and retain it for retries. |

Unknown fields and invalid types are rejected. Bodies do not accept a sandbox ID,
image, arbitrary registry name, environment overrides or a shell-command string.

### `POST /exec`

Calls the deployed worker through `.remote.aio()`. A normal command exit returns
HTTP 200, **including nonzero exits**:

```json
{
  "request_id": "a-new-unique-id-for-this-operation",
  "call_id": "fc-...",
  "status": "completed",
  "exit_code": 0,
  "stdout": "nix (...) ...\n",
  "stderr": "",
  "stdout_truncated": false,
  "stderr_truncated": false
}
```

Read `exit_code`; HTTP 200 does not mean the command succeeded. Output is capped
at 1 MiB per stream, drained concurrently, with truncation flags. An incomplete
trailing UTF-8 codepoint is omitted. This is buffered output, not streaming.

Validation gives 422; a conflicting request ID or unavailable/incomplete box gives
409; unavailable sandbox/registry gives 404. Execution-service failures have
`status: "failed"` with `error.code`, `error.message` and `error.http_status`.

Timeouts give 504 `exec_timeout`. Modal 1.6.3 can return `exit_code: -1` instead of
raising an exception on its exec deadline; the worker recognises that sentinel.
Partial output is retained when available, but not guaranteed on SDK exceptions.
Do not treat a timeout or disconnected client as permission to run the command again.

### `POST /spawn`

Calls the same worker through `.spawn.aio()` and returns HTTP 202 promptly:

```json
{
  "request_id": "a-new-unique-id-for-this-operation",
  "call_id": "fc-...",
  "status": "submitted",
  "poll_url": "/poll/fc-..."
}
```

The response also sets `Location` and `Retry-After: 1`. The worker runs independently
of the HTTP connection; FastAPI in-process background tasks are not used.

### `GET /poll/{call_id}`

| HTTP status | Meaning |
| --- | --- |
| 202 | `status: "pending"`; poll again. The handler uses `get.aio(timeout=0)`. |
| 200 | Terminal `status` and nested `result`. A failed command/worker can still be a successfully retrieved result. Inspect both status and exit code. |
| 404 | Unknown, unrelated or no-longer-indexed call. Arbitrary workspace FunctionCall results cannot be read here. |
| 410 | Modal reports an expired output. |
| 504 | Worker execution timeout: terminal failure, not pending. |
| 503 | Ownership/result lookup failed; retry polling rather than submitting another command. |

Completed polling does not consume the result or rerun the command. Service-owned
SDK and synchronous calls are indexed too, so their returned call IDs can be polled.
A terminal Modal worker failure returns a failed result, rather than suggesting
endless polling of a dead call.

## Retry safety and limits

Modal's `retries=0` disables application-exception retries, **not** infrastructure
replays after preemption or a container failure. Web functions do not accept the
`retries` parameter at all in SDK 1.6.3.

Before dispatch, the worker atomically claims `request:<request_id>` in
`box-exec-runs-v1` with `Dict.put(..., skip_if_exists=True)`:

- Same ID and same validated input, with a saved outcome: return that outcome.
- Same ID but different input, including timeout: reject with `request_conflict`.
- Same ID still claimed without an outcome: return `execution_unconfirmed` and the
  original `call_id`. Poll it and inspect the sandbox; do not rerun under a new ID
  unless you have established that doing so is safe.

No claim is automatically unlocked after an uncertain dispatch or failed result
save. Concurrent duplicate submissions can create multiple Modal function calls,
but only the winning claim may launch the sandbox command.

If you omit `request_id`, the HTTP handler uses its Modal input call ID; direct SDK
calls use their worker input call ID. A random UUID generated **inside** a handler
would change on a replay and defeat protection. Concurrent HTTP inputs have been
verified to receive distinct IDs. Stability across infrastructure retries relies
on Modal's input identity; forced preemption has not been tested. A fresh client
request is a new input: **always supply the same explicit ID when retrying it**.

This is not an exactly-once guarantee:

- Dict entries expire after seven **idle** days. Do not reuse old request IDs.
- Modal results expire seven days after completion. The ownership index can expire
  on a different clock, so old calls can give either 404 or 410.
- Inputs/results are retained by Modal, and bounded results are also cached in the
  private Dict. Treat arguments and output as workspace-accessible data.
- The four worker containers process one execution each. Four long jobs occupy all
  slots; synchronous requests may queue beyond their HTTP deadline. Prefer spawn
  when work is long or the service is busy.
- The web handler timeout is 140 seconds; Modal's HTTP limit is 150 seconds. Cold
  starts and queuing consume that budget too. A 90-second exec timeout is not a
  guarantee the synchronous HTTP response arrives before its deadline.
- The worker timeout is 3,720 seconds, above the maximum 3,600-second command
  timeout. Both worker and web functions scale to zero when idle; the web app is
  limited to two containers with 20 concurrent requests each.

## SDK and HTTP examples

SDK callers need the account's API credentials. On this machine, the existing
transport setup keeps TLS verification on while using system trust:

```python
import uuid
import modal
from box.modal_transport import connect

connect()  # Local process only; use dotenvx to inject credentials at startup.
execute = modal.Function.from_name("box-exec-api", "exec_command")
result = execute.remote({
    "name": "lf-box", "command": ["nix", "--version"],
    "request_id": uuid.uuid4().hex,
})
print(result)
# Background equivalent: call = execute.spawn(payload)
# Later: modal.FunctionCall.from_id(call.object_id).get(timeout=0)
```

An HTTPS client needs only the proxy credentials, not `.env` or the Modal SDK.
This example keeps secrets out of command-line arguments and disables redirects
so credentials cannot be forwarded elsewhere:

```python
import json, ssl, uuid
from pathlib import Path
import httpx

manifest = Path.home() / ".config/box/box-exec-api/imagent/main/deployment.json"
deployment = json.loads(manifest.read_text())
credentials = json.loads(Path(deployment["credentials_file"]).read_text())
headers = {"Modal-Key": credentials["token_id"], "Modal-Secret": credentials["token_secret"]}
with httpx.Client(verify=ssl.create_default_context(), follow_redirects=False, timeout=125) as client:
    response = client.post(deployment["url"] + "/exec", headers=headers, json={
        "name": "lf-box", "command": ["nix", "--version"],
        "request_id": uuid.uuid4().hex,
    })
    response.raise_for_status()
    print(response.json())
```

If proxy-token provisioning is interrupted, inspect this app's tokens in Modal's
workspace settings before deleting an incomplete `proxy.json` and retrying.
The helper deliberately does not mint another token after an uncertain creation.
For rotation, revoke the old token first, remove its local credential file, then
redeploy. Stopping the app in Modal leaves existing boxes and the local CLI intact.

## Verified vertical slices

- [x] **Remote function:** deployed `exec_command`, ran `nix --version` and a literal
  argv/cwd/stdout/stderr command with exit 23 against real `lf-box`.
- [x] **Authenticated HTTP:** deployed `POST /exec`, verified rejected credentials
  and real execution. No public unauthenticated deployment was used.
- [x] **Background calls:** deployed spawn/poll, observed pending → completed,
  background failure and repeat polling without another execution.
- [ ] **Operational proof:** final concurrency, timeout, duplicate, output-limit,
  packaging and regression checks; evidence in `remote-exec-verification.json`.

## Documentation sources

Consulted 5 November 2026, starting from [Modal's documentation index](https://modal.com/llms.txt).

- [Apps and deployment](https://modal.com/docs/guide/apps.md)
- [Managing deployments](https://modal.com/docs/guide/managing-deployments.md)
- [Functions](https://modal.com/docs/guide/functions.md) and [invocation methods](https://modal.com/docs/guide/function-invocation-methods.md)
- [Job processing and polling](https://modal.com/docs/guide/job-queue.md)
- [ASGI / FastAPI web functions](https://modal.com/docs/guide/webhooks.md)
- [Proxy authentication](https://modal.com/docs/guide/webhook-proxy-auth.md) and [proxy-token CLI](https://modal.com/docs/cli/latest/workspace.md)
- [HTTP timeouts](https://modal.com/docs/guide/webhook-timeouts.md)
- [Failures and infrastructure retries](https://modal.com/docs/guide/retries.md)
- [Dict limits, retention and atomic insertion](https://modal.com/docs/guide/dicts.md): current limit 100 MiB/object, under 5 MiB recommended; not 100 KiB.
- [FunctionCall](https://modal.com/docs/sdk/py/latest/FunctionCall.md) and [current call ID](https://modal.com/docs/sdk/py/latest/current_function_call_id.md)
- [Locked images with uv](https://modal.com/docs/guide/images.md)
- [FastAPI bodies](https://fastapi.tiangolo.com/tutorial/body/), [async handlers](https://fastapi.tiangolo.com/async/) and [errors](https://fastapi.tiangolo.com/tutorial/handling-errors/)

The installed Modal 1.6.3 source was also checked for atomic Dict insertion, Python
3.14 image support, proxy-token scope and the exec timeout sentinel. Seven-day
expiry and infrastructure-failure branches have offline tests; the live smoke
suite does not wait a week or deliberately crash the deployment.
