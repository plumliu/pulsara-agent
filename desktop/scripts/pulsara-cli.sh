#!/bin/sh
# Pulsara desktop command-line launcher. Debian installs only a symlink here.
launcher=$(readlink -f "$0") || exit 1
resources=$(dirname "$(dirname "$launcher")")
exec "$resources/python/bin/python3" -m pulsara_agent "$@"
