"""CPython 3.12 done task with its collection callback still queued."""

import asyncio
import json
from pathlib import Path
import tempfile
from relay_fixture import Fixture, rows


async def main():
    root = Path(tempfile.mkdtemp(prefix=".g0r4check-", dir="/home/plumwsl"))
    cfg = dict(
        directory=str(root / "scope"),
        record=str(root / "relay.jsonl"),
        anchor_record=str(root / "anchor.jsonl"),
        prefix=str(root / "commands"),
    )
    fixture = Fixture(cfg, "receiver")
    try:
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        fixture.tasks.add(task)
        task.add_done_callback(fixture.tasks.discard)
        # gather(done-task) can itself already be done; awaiting it need not run
        # the earlier collection callback. No timing sleep is injected here.
        await fixture.seal(False)
        ack = json.loads(fixture.file(".sealed.json").read_text())
        assert task.done() and ack["taskCount"] == ack["writerCount"] == 0
        print(
            json.dumps(
                dict(
                    kind="standard-asyncio-done-callback-queued",
                    passed=True,
                    ack=ack,
                    rows=rows(cfg["record"]),
                )
            )
        )
    except BaseException as error:
        print(
            json.dumps(
                dict(
                    kind="standard-asyncio-done-callback-queued",
                    passed=False,
                    error=repr(error),
                    tasks=[
                        dict(done=task.done(), cancelled=task.cancelled())
                        for task in fixture.tasks
                    ],
                    rows=rows(cfg["record"]),
                )
            )
        )
        raise
    finally:
        assert (
            root.resolve() == root
            and root.parent == Path("/home/plumwsl")
            and root.name.startswith(".g0r4check-")
        )
        for path in root.iterdir():
            assert path.is_file()
            path.unlink()
        root.rmdir()


if __name__ == "__main__":
    asyncio.run(main())
