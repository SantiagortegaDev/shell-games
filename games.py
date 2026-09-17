"""Game logic, no networking (the server orchestrates that)."""
import random

WIN_LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
]


def _config_base(config: str) -> str:
    """Strips off an optional ':'-separated match-rounds suffix, kept
    only for the client (round count, running score); the base is the
    part game logic actually cares about (mode/preset)."""
    return config.split(":", 1)[0]


def check_winner(board: list[str]) -> str | None:
    """Returns 'X', 'O', 'draw', or None if the game is still going."""
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None


class TicTacToe:
    kind = "ttt"

    def __init__(self, game_id: str, host_ws, host_name: str, config: str = "-") -> None:
        self.id = game_id
        self.config = config
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
        """Tries to play `cell` (0-8). Returns '' if valid, or an error."""
        if not self.started:
            return "the game doesn't have two players yet"
        symbol = self.symbol_for(ws)
        if symbol != self.turn:
            return "it's not your turn"
        if not (0 <= cell <= 8):
            return "invalid cell"
        if self.board[cell]:
            return "cell occupied"
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
HANGMAN_MODES = ("classic", "race", "turns")
HANGMAN_WORDS = [
    "cat", "dog", "house", "tree", "mountain", "sky", "beach", "fire",
    "cloud", "river", "sun", "moon", "star", "bridge", "road", "book",
    "train", "plane", "ship", "flower", "forest", "stone", "water",
    "wind", "winter", "summer", "music", "painting", "school", "castle",
]


class Hangman:
    """Mode 'classic': one player sets the word, the other guesses it.
    Mode 'race': the server picks a random word and both players guess
    independently at their own pace; whoever completes it first wins.
    Mode 'turns': one shared word, players alternate guesses (a hit
    grants another turn, a miss passes the turn); whoever's guess
    completes the word wins. A full-word guess is allowed too — wrong
    counts as exactly one miss."""

    kind = "hangman"

    def __init__(self, game_id: str, host_ws, host_name: str, config: str = "classic") -> None:
        self.id = game_id
        base = _config_base(config)
        self.mode = base if base in HANGMAN_MODES else "classic"
        self.config = config
        self.started = False
        self.progress: dict = {}  # ws -> {"guessed": set(), "misses": int}  (classic + race)
        if self.mode == "classic":
            self.word = ""
            self.word_set = False
            self.players = {host_ws: ("setter", host_name)}
        else:
            self.word = random.choice(HANGMAN_WORDS)
            self.word_set = True
            self.players: dict = {host_ws: ("P1", host_name)}
            if self.mode == "race":
                self.progress[host_ws] = {"guessed": set(), "misses": 0}
            else:  # turns
                self.guessed: set = set()
                self.misses = 0
                self.turn = host_ws

    def add_guest(self, guest_ws, guest_name: str) -> None:
        role = "P2" if self.mode != "classic" else "guesser"
        self.players[guest_ws] = (role, guest_name)
        if self.mode in ("classic", "race"):
            self.progress[guest_ws] = {"guessed": set(), "misses": 0}
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
        if self.mode == "classic":
            role = "setter" if not self.word_set else "guesser"
            for ws, (r, _) in self.players.items():
                if r == role:
                    return ws
            return None
        if self.mode == "race":
            return None  # everyone guesses at their own pace, no turns
        return self.turn

    def set_word(self, ws, word: str) -> str:
        if not self.started:
            return "the game doesn't have two players yet"
        if self.mode != "classic" or self.symbol_for(ws) != "setter":
            return "you're not the one setting the word"
        if self.word_set:
            return "the word is already set"
        word = word.strip().lower()
        if not word.isalpha() or not (3 <= len(word) <= 20):
            return "invalid word (letters only, 3 to 20)"
        self.word = word
        self.word_set = True
        return ""

    def is_done_for(self, ws) -> bool:
        p = self.progress[ws]
        return all(c in p["guessed"] for c in self.word) or p["misses"] >= HANGMAN_MAX_MISSES

    def guess(self, ws, text: str) -> str:
        if not self.word_set:
            return "your opponent hasn't chosen the word yet"
        if self.mode == "turns":
            return self._guess_turns(ws, text)
        if ws not in self.progress:
            return "you're not the guesser"
        if self.is_done_for(ws):
            return "you already finished this word"
        letter = text.strip().lower()
        if len(letter) != 1 or not letter.isalpha():
            return "invalid letter"
        p = self.progress[ws]
        if letter in p["guessed"]:
            return "you already tried that letter"
        p["guessed"].add(letter)
        if letter not in self.word:
            p["misses"] += 1
        return ""

    def _guess_turns(self, ws, text: str) -> str:
        if ws != self.turn:
            return "it's not your turn"
        text = text.strip().lower()
        if not text.isalpha():
            return "invalid guess"
        if len(text) == 1:
            if text in self.guessed:
                return "that letter was already tried"
            hit = text in self.word
            if hit:
                self.guessed.add(text)
        elif len(text) == len(self.word):
            hit = text == self.word
            if hit:
                self.guessed.update(self.word)
        else:
            return "invalid guess"
        if hit:
            return ""  # a hit grants another turn
        self.misses += 1
        self.turn = self.opponent_of(ws)
        return ""

    def revealed_for(self, ws) -> str:
        guessed = self.guessed if self.mode == "turns" else self.progress.get(ws, {}).get("guessed", set())
        return "".join(c if c in guessed else "_" for c in self.word)

    def misses_for(self, ws) -> int:
        if self.mode == "turns":
            return self.misses
        return self.progress.get(ws, {}).get("misses", 0)

    def result(self) -> str | None:
        """Returns the winning role/symbol, 'draw', or None if still going."""
        if not self.word_set:
            return None
        if self.mode == "classic":
            ws = next(iter(self.progress))
            p = self.progress[ws]
            if all(c in p["guessed"] for c in self.word):
                return "guesser"
            if p["misses"] >= HANGMAN_MAX_MISSES:
                return "setter"
            return None
        if self.mode == "turns":
            if all(c in self.guessed for c in self.word):
                return self.symbol_for(self.turn)
            return None
        # "race": ends as soon as anyone completes the word; a draw only
        # once everyone still in the running has maxed out their misses.
        for w in self.progress:
            if all(c in self.progress[w]["guessed"] for c in self.word):
                return self.symbol_for(w)
        if all(self.is_done_for(w) for w in self.progress):
            return "draw"
        return None


SHIP_SIZES = [4, 3, 2]  # "classic" preset, also the client's default
BOARD_SIZE = 8
BATTLESHIP_PRESETS = {
    "classic": [4, 3, 2],
    "fast": [3, 2],
    "big": [5, 4, 3, 2],
}


class Battleship:
    """Each player places their ships privately, then they take turns shooting."""

    kind = "battleship"

    def __init__(self, game_id: str, host_ws, host_name: str, config: str = "classic") -> None:
        self.id = game_id
        base = _config_base(config)
        self.preset = base if base in BATTLESHIP_PRESETS else "classic"
        self.config = config
        self.ship_sizes = BATTLESHIP_PRESETS[self.preset]
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
        return len(self.placed_ships.get(ws, {})) == len(self.ship_sizes)

    def both_ready(self) -> bool:
        return all(self.is_placement_done(w) for w in self.players)

    def place_ship(self, ws, ship_index: int, row: int, col: int, orientation: str) -> str:
        if not self.started:
            return "the game doesn't have two players yet"
        if self.battle:
            return "the shooting phase already started"
        placed = self.placed_ships[ws]
        if ship_index in placed:
            return "that ship is already placed"
        if not (0 <= ship_index < len(self.ship_sizes)):
            return "invalid ship"
        if orientation not in ("h", "v"):
            return "invalid orientation"
        size = self.ship_sizes[ship_index]
        occupied = {c for cells in placed.values() for c in cells}
        cells = []
        for i in range(size):
            r = row + (i if orientation == "v" else 0)
            c = col + (i if orientation == "h" else 0)
            if not (0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE):
                return "the ship goes off the board"
            idx = r * BOARD_SIZE + c
            if idx in occupied:
                return "it overlaps another ship"
            cells.append(idx)
        placed[ship_index] = cells
        return ""

    def start_battle(self) -> None:
        self.battle = True
        self.turn = next(iter(self.players))

    def shoot(self, ws, row: int, col: int) -> tuple[str, str, int | None]:
        """Returns (error, result, sunk_size). result is 'hit' or 'miss'
        when there's no error. A hit grants another turn; only a miss
        passes the turn to the opponent. sunk_size is the size of the ship
        just sunk by this shot, or None if none was sunk."""
        if not self.battle:
            return "ships are still being placed", "", None
        if ws != self.turn:
            return "it's not your turn", "", None
        if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
            return "invalid coordinate", "", None
        idx = row * BOARD_SIZE + col
        opponent = self.opponent_of(ws)
        if idx in self.shots_at[opponent]:
            return "you already shot there", "", None
        self.shots_at[opponent].add(idx)
        placed = self.placed_ships[opponent]
        ship_cells = {c for cells in placed.values() for c in cells}
        hit = idx in ship_cells
        sunk_size = None
        if hit:
            for ship_index, cells in placed.items():
                if idx in cells and set(cells).issubset(self.shots_at[opponent]):
                    sunk_size = self.ship_sizes[ship_index]
                    break
        else:
            self.turn = opponent
        return "", ("hit" if hit else "miss"), sunk_size

    def check_winner(self):
        """Returns the winning symbol, or None if still going."""
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
