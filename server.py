#!/usr/bin/env python3
"""Servidor de chat multiplayer por WebSocket (hito 1)."""
import asyncio
import logging
import sys

import websockets
from websockets.asyncio.server import ServerConnection, serve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("server")

HOST = "0.0.0.0"
PORT = 8888

players: dict[ServerConnection, str] = {}


async def broadcast(text: str, exclude: ServerConnection | None = None) -> None:
    for ws in list(players):
        if ws is exclude:
            continue
        try:
            await ws.send(text)
        except websockets.ConnectionClosed:
            pass


async def handler(ws: ServerConnection) -> None:
    await ws.send("Nombre: ")
    try:
        name = (await ws.recv()).strip()
    except websockets.ConnectionClosed:
        return

    if not name:
        name = f"anon{id(ws) % 1000}"
    base, n = name, 2
    while name in players.values():
        name = f"{base}{n}"
        n += 1

    players[ws] = name
    log.info("conectado: %s (%s) - %d en linea", name, ws.remote_address, len(players))
    await ws.send(f"Conectado como {name}. Comandos: /who, /quit")
    await broadcast(f"* {name} se unio", exclude=ws)

    try:
        async for raw in ws:
            msg = raw.strip()
            if not msg:
                continue
            if msg == "/quit":
                break
            if msg == "/who":
                who = ", ".join(players.values())
                await ws.send(f"* conectados ({len(players)}): {who}")
                continue
            log.info("%s: %s", name, msg)
            await broadcast(f"{name}: {msg}", exclude=ws)
    except websockets.ConnectionClosed:
        pass
    finally:
        players.pop(ws, None)
        log.info("desconectado: %s - %d en linea", name, len(players))
        await broadcast(f"* {name} se fue")


async def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    async with serve(handler, HOST, port):
        log.info("escuchando en %s:%d", HOST, port)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
