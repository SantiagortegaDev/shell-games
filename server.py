#!/usr/bin/env python3
"""Servidor de chat multiplayer por WebSocket, con salas de 3 en raya."""
import asyncio
import logging
import random
import re
import string
import sys
import time

import websockets
from websockets.asyncio.server import ServerConnection, serve

from games import Battleship, Hangman, TicTacToe, check_winner

Game = TicTacToe | Hangman | Battleship
GAME_CLASSES = {"ttt": TicTacToe, "hangman": Hangman, "battleship": Battleship}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("server")

HOST = "0.0.0.0"
PORT = 8888
CODE_ALPHABET = string.ascii_uppercase + string.digits
INVITE_RATE_LIMIT = 3
INVITE_RATE_WINDOW = 60.0
INACTIVITY_TIMEOUT = 120.0

players: dict[ServerConnection, str] = {}
games: dict[str, Game] = {}
codes: dict[str, str] = {}  # codigo corto -> game_id
invite_times: dict[ServerConnection, list[float]] = {}
watchdogs: dict[str, asyncio.Task] = {}  # game_id -> timer de inactividad


async def broadcast(text: str, exclude: ServerConnection | None = None) -> None:
    for ws in list(players):
        if ws is exclude:
            continue
        try:
            await ws.send(text)
        except websockets.ConnectionClosed:
            pass


async def send(ws: ServerConnection, text: str) -> None:
    try:
        await ws.send(text)
    except websockets.ConnectionClosed:
        pass


def new_game_id() -> str:
    gid = "".join(random.choices(CODE_ALPHABET, k=8))
    while gid in games:
        gid = "".join(random.choices(CODE_ALPHABET, k=8))
    return gid


def new_code() -> str:
    code = "".join(random.choices(CODE_ALPHABET, k=4))
    while code in codes:
        code = "".join(random.choices(CODE_ALPHABET, k=4))
    return code


def find_ws_by_name(name: str) -> ServerConnection | None:
    for ws, pname in players.items():
        if pname == name:
            return ws
    return None


def is_in_active_game(ws: ServerConnection) -> bool:
    return any(ws in g.players and g.started for g in games.values())


def invite_rate_ok(ws: ServerConnection) -> bool:
    """Maximo INVITE_RATE_LIMIT invitaciones por nombre cada INVITE_RATE_WINDOW segundos."""
    now = time.monotonic()
    times = invite_times.setdefault(ws, [])
    times[:] = [t for t in times if now - t < INVITE_RATE_WINDOW]
    if len(times) >= INVITE_RATE_LIMIT:
        return False
    times.append(now)
    return True


def cancel_watchdog(gid: str) -> None:
    task = watchdogs.pop(gid, None)
    if task is not None:
        task.cancel()


def schedule_watchdog(gid: str) -> None:
    cancel_watchdog(gid)
    watchdogs[gid] = asyncio.create_task(_watchdog(gid))


async def _watchdog(gid: str) -> None:
    try:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
    except asyncio.CancelledError:
        return
    game = games.get(gid)
    if game is None or not game.started:
        return
    loser_ws = game.whose_turn_ws()
    if loser_ws is None:
        return
    winner_symbol = game.symbol_for(game.opponent_of(loser_ws))
    for ws in game.players:
        await send(ws, f"!over {gid} {winner_symbol} timeout")
    games.pop(gid, None)
    watchdogs.pop(gid, None)


async def cleanup_games_for(ws: ServerConnection) -> None:
    for gid, game in list(games.items()):
        if ws not in game.players:
            continue
        opponent = game.opponent_of(ws)
        if opponent is not None:
            await send(opponent, f"!opponent_left {gid}")
        cancel_watchdog(gid)
        games.pop(gid, None)
        for code, mapped_gid in list(codes.items()):
            if mapped_gid == gid:
                codes.pop(code, None)


async def broadcast_board(game: TicTacToe) -> None:
    result = check_winner(game.board)
    for ws in game.players:
        await send(ws, f"!board {game.id} {game.board_str()} {game.turn}")
    if result is not None:
        cancel_watchdog(game.id)
        for ws in game.players:
            await send(ws, f"!over {game.id} {result} normal")
        games.pop(game.id, None)
    else:
        schedule_watchdog(game.id)


async def handle_game_command(ws: ServerConnection, name: str, msg: str) -> bool:
    """Procesa comandos de juego. Devuelve True si `msg` era uno de ellos."""
    parts = msg.split()
    cmd = parts[0]

    if cmd == "/play" and len(parts) == 3:
        if is_in_active_game(ws):
            await send(ws, "!error ya estas jugando una partida")
            return True
        if not invite_rate_ok(ws):
            await send(ws, "!error estas invitando muy rapido, espera un poco")
            return True
        target_name, kind = parts[1], parts[2]
        game_cls = GAME_CLASSES.get(kind)
        if game_cls is None:
            await send(ws, "!error juego invalido")
            return True
        target_ws = find_ws_by_name(target_name)
        if target_ws is None or target_ws is ws:
            await send(ws, "!error jugador no encontrado")
            return True
        gid = new_game_id()
        games[gid] = game_cls(gid, ws, name)
        await send(target_ws, f"!invite {gid} {name} {kind}")
        await send(ws, f"!invited {gid} {target_name}")
        return True

    if cmd == "/code" and len(parts) == 2:
        if is_in_active_game(ws):
            await send(ws, "!error ya estas jugando una partida")
            return True
        kind = parts[1]
        game_cls = GAME_CLASSES.get(kind)
        if game_cls is None:
            await send(ws, "!error juego invalido")
            return True
        gid = new_game_id()
        games[gid] = game_cls(gid, ws, name)
        code = new_code()
        codes[code] = gid
        await send(ws, f"!code {gid} {code}")
        return True

    if cmd == "/join" and len(parts) == 2:
        if is_in_active_game(ws):
            await send(ws, "!error ya estas jugando una partida")
            return True
        code = parts[1].upper()
        gid = codes.get(code)
        game = games.get(gid) if gid else None
        if game is None or game.started:
            await send(ws, "!error codigo invalido")
            return True
        codes.pop(code, None)
        game.add_guest(ws, name)
        for pws in game.players:
            opp = game.name_for(game.opponent_of(pws))
            await send(pws, f"!start {gid} {opp} {game.symbol_for(pws)} {game.kind}")
        if game.kind != "battleship":
            schedule_watchdog(gid)
        return True

    if cmd == "/accept" and len(parts) == 2:
        if is_in_active_game(ws):
            await send(ws, "!error ya estas jugando una partida")
            return True
        gid = parts[1]
        game = games.get(gid)
        if game is None or game.started or ws in game.players:
            await send(ws, "!error invitacion invalida")
            return True
        game.add_guest(ws, name)
        for pws in game.players:
            opp = game.name_for(game.opponent_of(pws))
            await send(pws, f"!start {gid} {opp} {game.symbol_for(pws)} {game.kind}")
        if game.kind != "battleship":
            schedule_watchdog(gid)
        return True

    if cmd == "/decline" and len(parts) == 2:
        gid = parts[1]
        game = games.pop(gid, None)
        if game is not None:
            host = next(iter(game.players))
            await send(host, f"!declined {gid} {name}")
        return True

    if cmd == "/move" and len(parts) == 3:
        gid = parts[1]
        game = games.get(gid)
        if game is None or ws not in game.players or game.kind != "ttt":
            await send(ws, "!error partida invalida")
            return True
        try:
            cell = int(parts[2])
        except ValueError:
            await send(ws, "!error celda invalida")
            return True
        error = game.move(ws, cell)
        if error:
            await send(ws, f"!error {error}")
            return True
        await broadcast_board(game)
        return True

    if cmd == "/hangword" and len(parts) >= 3:
        gid = parts[1]
        game = games.get(gid)
        if game is None or ws not in game.players or game.kind != "hangman":
            await send(ws, "!error partida invalida")
            return True
        word = " ".join(parts[2:])
        error = game.set_word(ws, word)
        if error:
            await send(ws, f"!error {error}")
            return True
        for pws in game.players:
            await send(pws, f"!hm_state {gid} {game.revealed()} {game.misses}")
        schedule_watchdog(gid)
        return True

    if cmd == "/guess" and len(parts) == 3:
        gid = parts[1]
        game = games.get(gid)
        if game is None or ws not in game.players or game.kind != "hangman":
            await send(ws, "!error partida invalida")
            return True
        error = game.guess(ws, parts[2])
        if error:
            await send(ws, f"!error {error}")
            return True
        result = game.result()
        if result is not None:
            cancel_watchdog(gid)
            for pws in game.players:
                await send(pws, f"!hm_state {gid} {game.word} {game.misses}")
                await send(pws, f"!over {gid} {result} normal")
            games.pop(gid, None)
        else:
            for pws in game.players:
                await send(pws, f"!hm_state {gid} {game.revealed()} {game.misses}")
            schedule_watchdog(gid)
        return True

    if cmd == "/place" and len(parts) == 6:
        gid = parts[1]
        game = games.get(gid)
        if game is None or ws not in game.players or game.kind != "battleship":
            await send(ws, "!error partida invalida")
            return True
        try:
            ship_index, row, col = int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            await send(ws, "!error datos invalidos")
            return True
        orientation = parts[5]
        error = game.place_ship(ws, ship_index, row, col, orientation)
        if error:
            await send(ws, f"!error {error}")
            return True
        for pws in game.players:
            await send(pws, f"!bs_state {gid} {game.own_board_str(pws)} {game.tracking_board_str(pws)} placing -")
        if game.both_ready():
            game.start_battle()
            for pws in game.players:
                await send(pws, f"!bs_state {gid} {game.own_board_str(pws)} {game.tracking_board_str(pws)} battle {game.symbol_for(game.turn)}")
            schedule_watchdog(gid)
        return True

    if cmd == "/shot" and len(parts) == 4:
        gid = parts[1]
        game = games.get(gid)
        if game is None or ws not in game.players or game.kind != "battleship":
            await send(ws, "!error partida invalida")
            return True
        try:
            row, col = int(parts[2]), int(parts[3])
        except ValueError:
            await send(ws, "!error coordenada invalida")
            return True
        error, _result = game.shoot(ws, row, col)
        if error:
            await send(ws, f"!error {error}")
            return True
        winner = game.check_winner()
        if winner is not None:
            cancel_watchdog(gid)
            for pws in game.players:
                await send(pws, f"!bs_state {gid} {game.own_board_str(pws)} {game.tracking_board_str(pws)} battle -")
                await send(pws, f"!over {gid} {winner} normal")
            games.pop(gid, None)
        else:
            for pws in game.players:
                await send(pws, f"!bs_state {gid} {game.own_board_str(pws)} {game.tracking_board_str(pws)} battle {game.symbol_for(game.turn)}")
            schedule_watchdog(gid)
        return True

    if cmd == "/forfeit" and len(parts) == 2:
        gid = parts[1]
        game = games.get(gid)
        if game is None or ws not in game.players or not game.started:
            await send(ws, "!error partida invalida")
            return True
        cancel_watchdog(gid)
        winner_symbol = game.symbol_for(game.opponent_of(ws))
        for pws in game.players:
            await send(pws, f"!over {gid} {winner_symbol} forfeit")
        games.pop(gid, None)
        return True

    return False


async def handler(ws: ServerConnection) -> None:
    await ws.send("Nombre: ")
    try:
        name = (await ws.recv()).strip()
    except websockets.ConnectionClosed:
        return

    # el protocolo es texto separado por espacios: un nombre con espacios
    # rompe el parseo de !start/!invite/etc. en el cliente, asi que se
    # sanea aca, en el unico lugar donde nace el nombre.
    name = re.sub(r"\s+", "_", name)[:20]
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
            if msg.startswith("/") and await handle_game_command(ws, name, msg):
                continue
            log.info("%s: %s", name, msg)
            await broadcast(f"{name}: {msg}", exclude=ws)
    except websockets.ConnectionClosed:
        pass
    finally:
        players.pop(ws, None)
        invite_times.pop(ws, None)
        await cleanup_games_for(ws)
        log.info("desconectado: %s - %d en linea", name, len(players))
        await broadcast(f"* {name} se fue")


async def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    async with serve(handler, HOST, port):
        log.info("escuchando en %s:%d", HOST, port)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
