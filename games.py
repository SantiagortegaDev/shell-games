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
