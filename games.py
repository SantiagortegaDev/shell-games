"""Logica de 3 en raya, sin nada de red (el server la orquesta)."""

WIN_LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
]


def check_winner(board: list[str]) -> str | None:
    """Devuelve 'X', 'O', 'draw' o None si la partida sigue."""
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None


class TicTacToe:
    kind = "ttt"

    def __init__(self, game_id: str, host_ws, host_name: str) -> None:
        self.id = game_id
        self.players: dict = {host_ws: ("X", host_name)}
        self.board: list[str] = [""] * 9
        self.turn = "X"
        self.started = False

    def add_guest(self, guest_ws, guest_name: str) -> None:
        self.players[guest_ws] = ("O", guest_name)
        self.started = True

    def symbol_for(self, ws) -> str:
        return self.players[ws][0]

    def name_for(self, ws) -> str:
        return self.players[ws][1]

    def opponent_of(self, ws):
        for other in self.players:
            if other is not ws:
                return other
        return None

    def move(self, ws, cell: int) -> str:
        """Intenta jugar `cell` (0-8). Devuelve '' si fue valida, o un error."""
        if not self.started:
            return "la partida todavia no tiene dos jugadores"
        symbol = self.symbol_for(ws)
        if symbol != self.turn:
            return "no es tu turno"
        if not (0 <= cell <= 8):
            return "celda invalida"
        if self.board[cell]:
            return "celda ocupada"
        self.board[cell] = symbol
        self.turn = "O" if symbol == "X" else "X"
        return ""

    def board_str(self) -> str:
        return "".join(c or "." for c in self.board)

    def whose_turn_ws(self):
        for ws, (sym, _) in self.players.items():
            if sym == self.turn:
                return ws
        return None


HANGMAN_MAX_MISSES = 6


class Hangman:
    """Uno pone la palabra (setter), el otro adivina letras (guesser)."""

    kind = "hangman"

    def __init__(self, game_id: str, host_ws, host_name: str) -> None:
        self.id = game_id
        self.players: dict = {host_ws: ("setter", host_name)}
        self.word = ""
        self.word_set = False
        self.guessed: set[str] = set()
        self.misses = 0
        self.started = False

    def add_guest(self, guest_ws, guest_name: str) -> None:
        self.players[guest_ws] = ("guesser", guest_name)
        self.started = True

    def symbol_for(self, ws) -> str:
        return self.players[ws][0]

    def name_for(self, ws) -> str:
        return self.players[ws][1]

    def opponent_of(self, ws):
        for other in self.players:
            if other is not ws:
                return other
        return None

    def whose_turn_ws(self):
        role = "setter" if not self.word_set else "guesser"
        for ws, (r, _) in self.players.items():
            if r == role:
                return ws
        return None

    def set_word(self, ws, word: str) -> str:
        if self.symbol_for(ws) != "setter":
            return "no sos quien pone la palabra"
        if self.word_set:
            return "la palabra ya esta puesta"
        word = word.strip().lower()
        if not word.isalpha() or not (3 <= len(word) <= 20):
            return "palabra invalida (solo letras, 3 a 20)"
        self.word = word
        self.word_set = True
        return ""

    def guess(self, ws, letter: str) -> str:
        if not self.word_set:
            return "el rival todavia no eligio la palabra"
        if self.symbol_for(ws) != "guesser":
            return "no sos quien adivina"
        letter = letter.strip().lower()
        if len(letter) != 1 or not letter.isalpha():
            return "letra invalida"
        if letter in self.guessed:
            return "ya intentaste esa letra"
        self.guessed.add(letter)
        if letter not in self.word:
            self.misses += 1
        return ""

    def revealed(self) -> str:
        return "".join(c if c in self.guessed else "_" for c in self.word)

    def result(self) -> str | None:
        """Devuelve 'guesser', 'setter' o None si la partida sigue."""
        if not self.word_set:
            return None
        if all(c in self.guessed for c in self.word):
            return "guesser"
        if self.misses >= HANGMAN_MAX_MISSES:
            return "setter"
        return None


SHIP_SIZES = [4, 3, 2]
BOARD_SIZE = 8


class Battleship:
    """Cada jugador coloca sus barcos en privado, despues disparan por turnos."""

    kind = "battleship"

    def __init__(self, game_id: str, host_ws, host_name: str) -> None:
        self.id = game_id
        self.players: dict = {host_ws: ("P1", host_name)}
        self.started = False
        self.battle = False
        self.turn = None
        self.placed_ships: dict = {}
        self.shots_at: dict = {}

    def add_guest(self, guest_ws, guest_name: str) -> None:
        self.players[guest_ws] = ("P2", guest_name)
        self.started = True
        for ws in self.players:
            self.placed_ships[ws] = {}
            self.shots_at[ws] = set()

    def symbol_for(self, ws) -> str:
        return self.players[ws][0]

    def name_for(self, ws) -> str:
        return self.players[ws][1]

    def opponent_of(self, ws):
        for other in self.players:
            if other is not ws:
                return other
        return None

    def whose_turn_ws(self):
        return self.turn if self.battle else None

    def is_placement_done(self, ws) -> bool:
        return len(self.placed_ships.get(ws, {})) == len(SHIP_SIZES)

    def both_ready(self) -> bool:
        return all(self.is_placement_done(w) for w in self.players)

    def place_ship(self, ws, ship_index: int, row: int, col: int, orientation: str) -> str:
        if self.battle:
            return "ya empezo la fase de disparos"
        placed = self.placed_ships[ws]
        if ship_index in placed:
            return "ese barco ya esta colocado"
        if not (0 <= ship_index < len(SHIP_SIZES)):
            return "barco invalido"
        if orientation not in ("h", "v"):
            return "orientacion invalida"
        size = SHIP_SIZES[ship_index]
        occupied = {c for cells in placed.values() for c in cells}
        cells = []
        for i in range(size):
            r = row + (i if orientation == "v" else 0)
            c = col + (i if orientation == "h" else 0)
            if not (0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE):
                return "el barco se sale del tablero"
            idx = r * BOARD_SIZE + c
            if idx in occupied:
                return "se superpone con otro barco"
            cells.append(idx)
        placed[ship_index] = cells
        return ""

    def start_battle(self) -> None:
        self.battle = True
        self.turn = next(iter(self.players))

    def shoot(self, ws, row: int, col: int) -> tuple[str, str, int | None]:
        """Devuelve (error, resultado, tamano_hundido). resultado: 'hit' o
        'miss' si no hubo error. Acertar da otro turno; solo fallar pasa el
        turno al rival. tamano_hundido es el tamano del barco recien
        hundido con este disparo, o None si no se hundio ninguno."""
        if not self.battle:
            return "todavia se estan colocando los barcos", "", None
        if ws != self.turn:
            return "no es tu turno", "", None
        if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
            return "coordenada invalida", "", None
        idx = row * BOARD_SIZE + col
        opponent = self.opponent_of(ws)
        if idx in self.shots_at[opponent]:
            return "ya disparaste ahi", "", None
        self.shots_at[opponent].add(idx)
        placed = self.placed_ships[opponent]
        ship_cells = {c for cells in placed.values() for c in cells}
        hit = idx in ship_cells
        sunk_size = None
        if hit:
            for ship_index, cells in placed.items():
                if idx in cells and set(cells).issubset(self.shots_at[opponent]):
                    sunk_size = SHIP_SIZES[ship_index]
                    break
        else:
            self.turn = opponent
        return "", ("hit" if hit else "miss"), sunk_size

    def check_winner(self):
        """Devuelve el simbolo ganador o None si sigue."""
        for ws in self.players:
            opponent = self.opponent_of(ws)
            ship_cells = {c for cells in self.placed_ships[opponent].values() for c in cells}
            if ship_cells and ship_cells.issubset(self.shots_at[opponent]):
                return self.symbol_for(ws)
        return None

    def own_board_str(self, ws) -> str:
        ships = {c for cells in self.placed_ships[ws].values() for c in cells}
        shots = self.shots_at[ws]
        out = []
        for idx in range(BOARD_SIZE * BOARD_SIZE):
            if idx in shots and idx in ships:
                out.append("X")
            elif idx in shots:
                out.append("o")
            elif idx in ships:
                out.append("S")
            else:
                out.append(".")
        return "".join(out)

    def tracking_board_str(self, ws) -> str:
        opponent = self.opponent_of(ws)
        ships = {c for cells in self.placed_ships[opponent].values() for c in cells}
        shots = self.shots_at[opponent]
        out = []
        for idx in range(BOARD_SIZE * BOARD_SIZE):
            if idx in shots and idx in ships:
                out.append("X")
            elif idx in shots:
                out.append("o")
            else:
                out.append(".")
        return "".join(out)
