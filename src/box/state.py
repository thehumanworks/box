"""Account-scoped records and atomic cross-client operation locks on Modal."""

import os
import socket
from contextlib import contextmanager
from datetime import UTC, datetime

import modal

from box.modal_transport import connect

APP_NAME = "box-dev"
REGISTRY_NAME = "box-dev-registry-v1"


def timestamp():
    return datetime.now(UTC).isoformat()


class Registry:
    def __init__(self, data=None):
        if data is None:
            # Fail before taking locks or starting billable work when API access
            # is blocked. Also configures trust for all subsequent SDK channels.
            connect()
        self.data = (
            data
            if data is not None
            else modal.Dict.from_name(REGISTRY_NAME, create_if_missing=True)
        )

    def get(self, name):
        return self.data.get("box:" + name)

    def save(self, record):
        record["updated_at"] = timestamp()
        self.data["box:" + record["name"]] = record

    def delete(self, name):
        self.data.pop("box:" + name)

    def all(self):
        return sorted(
            [value for key, value in self.data.items() if key.startswith("box:")],
            key=lambda item: item["name"],
        )

    @contextmanager
    def lock(self, name):
        key = "lock:" + name
        owner = {"host": socket.gethostname(), "pid": os.getpid(), "since": timestamp()}
        if not self.data.put(key, owner, skip_if_exists=True):
            raise ValueError(
                f"Another operation holds {name!r}: {self.data.get(key)}. "
                f"If it crashed, run: box unlock {name} --force"
            )
        try:
            yield
        finally:
            self.data.pop(key)

    def unlock(self, name):
        return self.data.pop("lock:" + name, None)
