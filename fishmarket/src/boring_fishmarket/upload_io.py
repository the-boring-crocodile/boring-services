"""Upload-owned disk work: settle a thread before aborting or closing its file."""
import asyncio
import os

async def disk_call(function, *args):
    work = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # Cancellation cannot interrupt an OS write/fsync. Keep ownership until
        # it finishes, including repeated cancellation, before upload abort/close.
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                pass
            except Exception:
                break
        if not work.cancelled():
            work.exception()
        raise

def append(handle, digest, piece):
    handle.write(piece)
    digest.update(piece)

def durable(handle):
    handle.flush()
    os.fsync(handle.fileno())
