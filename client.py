#!/usr/bin/env python3
"""Cliente de terminal para el chat (hito 1)."""
import asyncio
import sys

import websockets
from websockets.asyncio.client import connect


async def reader(ws) -> None:
    async for msg in ws:
        print(msg)


async def writer(ws) -> None:
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.rstrip("\n")
        await ws.send(line)
        if line == "/quit":
            break


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8888"
    async with connect(url) as ws:
        reader_task = asyncio.create_task(reader(ws))
        writer_task = asyncio.create_task(writer(ws))
        _, pending = await asyncio.wait(
            {reader_task, writer_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, websockets.ConnectionClosed):
        pass
