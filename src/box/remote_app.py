"""Modal deployment: authenticated SDK execution, then optional HTTP endpoints.

Deploy with scripts/deploy_remote.py so local enterprise TLS trust is configured.
Only Python source and the locked dependency files enter the image, never .env.
"""

import modal

from box.remote_constants import APP_NAME, MAX_TIMEOUT, RUNS_NAME

image = (
    modal.Image.debian_slim(python_version="3.14")
    .uv_sync(extras=["remote"], extra_options="--no-dev", uv_version="0.13.5")
    .add_local_python_source("box")
)

# pydantic and FastAPI (the "remote" extra) are installed in the image from uv.lock,
# not necessarily in the local deploy environment. Modal records local import errors
# here and re-raises them inside a container running this image.
with image.imports():
    from pydantic import ValidationError

    from box.remote_exec import ExecRequest, execute_claimed, failure

app = modal.App(APP_NAME)
runs = modal.Dict.from_name(RUNS_NAME, create_if_missing=True)


@app.function(image=image, timeout=MAX_TIMEOUT + 120, retries=0, max_containers=4)
def exec_command(payload: dict) -> dict:
    call_id = modal.current_function_call_id()
    try:
        request = ExecRequest.model_validate(payload)
    except ValidationError:
        return failure(None, call_id, "invalid_request", "Invalid execution request.", 422)
    try:
        return execute_claimed(request, call_id, runs)
    except Exception:
        # Claim/ownership storage can fail before execute_claimed reaches its
        # execution guard. Return a terminal outcome, not an exception that
        # would make pollers keep treating a finished call as a transport error.
        return failure(
            request.request_id or call_id,
            call_id,
            "service_error",
            "Execution could not be confirmed. Inspect the sandbox before starting another call.",
            503,
        )


@app.function(image=image, timeout=140, max_containers=2)
@modal.concurrent(max_inputs=20)
@modal.asgi_app(requires_proxy_auth=True)
def web():
    from box.http_api import create_api

    return create_api(exec_command, runs)
