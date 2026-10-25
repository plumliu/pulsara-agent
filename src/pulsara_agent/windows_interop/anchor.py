"""Job-owned WSL anchor: pin this invocation's actual interop object once."""

from __future__ import annotations

import array
import os
import socket
import sys


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("expected the private SCM control socket")
    descriptor = os.open(os.environ["WSL_INTEROP"], os.O_PATH | os.O_CLOEXEC)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.connect(sys.argv[1])
            sent = channel.sendmsg(
                [b"F"],
                [
                    (
                        socket.SOL_SOCKET,
                        socket.SCM_RIGHTS,
                        array.array("i", [descriptor]),
                    )
                ],
            )
            if sent != 1:
                raise RuntimeError("interop anchor did not transfer its descriptor")
        # Normal EOF comes from the exact Windows owner, never a lifetime timer.
        sys.stdin.buffer.read()
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
