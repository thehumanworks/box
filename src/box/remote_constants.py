"""Exec-service names and limits, importable without the "remote" extra.

scripts/deploy_remote.py imports box.remote_app locally, where pydantic and FastAPI
need not be installed; those dependencies are installed only in the Modal image.
"""

APP_NAME = "box-exec-api"
RUNS_NAME = "box-exec-runs-v1"
MAX_TIMEOUT = 3600
