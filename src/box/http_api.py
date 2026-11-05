"""FastAPI routes. Modal's ASGI proxy authenticates before any route is reached."""

import modal
from fastapi import FastAPI, Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from box.remote_exec import SYNC_TIMEOUT, ExecRequest, failure


def response(outcome):
    status = 200 if outcome["status"] == "completed" else outcome["error"]["http_status"]
    return JSONResponse(outcome, status_code=status)


def with_request_id(request, call_id_factory):
    # A fresh UUID generated inside a web handler would change if Modal replayed
    # that handler after a crash, defeating the worker's duplicate-dispatch guard.
    request_id = request.request_id or call_id_factory()
    if not request_id:
        raise RuntimeError("A stable Modal request identity is required before dispatch")
    return request.model_copy(update={"request_id": request_id})


def create_api(
    exec_function,
    runs,
    call_factory=modal.FunctionCall.from_id,
    request_id_factory=modal.current_function_call_id,
):
    api = FastAPI(title="Box remote exec", version="1")

    @api.exception_handler(RequestValidationError)
    async def invalid_request(_request, error):
        # Do not echo command arguments (which may contain secrets) in diagnostics.
        return JSONResponse(
            {
                "status": "failed",
                "error": {
                    "code": "invalid_request",
                    "http_status": 422,
                    "details": [
                        {"loc": e["loc"], "type": e["type"], "msg": e["msg"]}
                        for e in error.errors()
                    ],
                },
            },
            status_code=422,
        )

    @api.post("/exec")
    async def execute(request: ExecRequest):
        request = with_request_id(request, request_id_factory)
        if request.timeout > SYNC_TIMEOUT:
            return response(
                failure(
                    request.request_id,
                    None,
                    "use_spawn",
                    f"Synchronous timeout must be at most {SYNC_TIMEOUT}s; use /spawn and /poll.",
                    422,
                )
            )
        try:
            result = await exec_function.remote.aio(request.model_dump())
        except modal.exception.FunctionTimeoutError:
            return response(
                failure(
                    request.request_id,
                    None,
                    "worker_timeout",
                    "Worker timed out. Inspect the sandbox before starting another call.",
                    504,
                )
            )
        except Exception:
            return response(
                failure(
                    request.request_id,
                    None,
                    "dispatch_unconfirmed",
                    "Dispatch or result retrieval failed. Reuse the same request_id; "
                    "a disconnected request is not proof the command did not run.",
                    503,
                )
            )
        return response(result)

    @api.post("/spawn", status_code=202)
    async def spawn(request: ExecRequest):
        request = with_request_id(request, request_id_factory)
        call_id = None
        try:
            call = await exec_function.spawn.aio(request.model_dump())
            call_id = call.object_id
            await runs.put.aio("call:" + call_id, {"request_id": request.request_id})
        except Exception:
            return response(
                failure(
                    request.request_id,
                    call_id,
                    "dispatch_unconfirmed",
                    "Submission could not be confirmed. Reuse the same request_id, not a new one.",
                    503,
                )
            )
        return JSONResponse(
            {
                "request_id": request.request_id,
                "call_id": call_id,
                "status": "submitted",
                "poll_url": f"/poll/{call_id}",
            },
            status_code=202,
            headers={"Location": f"/poll/{call_id}", "Retry-After": "1"},
        )

    @api.get("/poll/{call_id}")
    async def poll(call_id: str = Path(pattern=r"^fc-[A-Za-z0-9]+$", max_length=128)):
        try:
            owned = await runs.get.aio("call:" + call_id)
        except Exception:
            return response(
                failure(
                    None,
                    call_id,
                    "poll_error",
                    "Could not verify call ownership; retry polling.",
                    503,
                )
            )
        if owned is None:
            return response(
                failure(
                    None,
                    call_id,
                    "unknown_call",
                    "No execution owned by this service has that ID.",
                    404,
                )
            )
        try:
            call = call_factory(call_id)
            result = await call.get.aio(timeout=0)
        except modal.exception.OutputExpiredError:
            return JSONResponse({"call_id": call_id, "status": "expired"}, status_code=410)
        except TimeoutError:
            return JSONResponse(
                {"call_id": call_id, "status": "pending"},
                status_code=202,
                headers={"Retry-After": "1"},
            )
        except modal.exception.NotFoundError:
            return response(
                failure(None, call_id, "unknown_call", "Execution no longer found.", 404)
            )
        except modal.exception.FunctionTimeoutError:
            return response(
                failure(None, call_id, "worker_timeout", "Execution worker timed out.", 504)
            )
        except modal.exception.RemoteError:
            return {
                "call_id": call_id,
                "status": "failed",
                "result": failure(
                    owned["request_id"],
                    call_id,
                    "worker_failed",
                    "The worker failed and the command outcome is unknown. "
                    "Inspect the sandbox; do not submit another execution blindly.",
                    503,
                ),
            }
        except Exception:
            return response(
                failure(
                    None,
                    call_id,
                    "poll_error",
                    "Could not retrieve the result. Retry polling, not command submission.",
                    503,
                )
            )
        # Polling succeeded even if the command/worker failed. Keep its terminal
        # outcome distinct from transport failure and retain the remote exit code.
        return {"call_id": call_id, "status": result["status"], "result": result}

    return api
