#!/bin/bash
set -euo pipefail
# ENTRYPOINT clears BASH_ENV before this shell starts. Keep user profiles out of
# service startup; terminals launched by Rex load them in their own shells.
exec /usr/local/bin/python /opt/box/boot.py
