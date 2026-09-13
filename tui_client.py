#!/usr/bin/env python3
"""Cliente TUI para Shell Games (Textual): menu, chat status y 3 en raya."""
import asyncio
import os
import random
import re
import sys
import time

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, Static
from websockets.asyncio.client import connect

from games import BATTLESHIP_PRESETS, BOARD_SIZE, HANGMAN_MAX_MISSES, HANGMAN_MODES, WIN_LINES

DEFAULT_URL = "wss://shellgames.santiagortega.dev"

GAME_NAMES = {"ttt": "3 en raya", "hangman": "Ahorcado", "battleship": "Batalla naval"}

BANNER = r"""   _____ __         ____   ______
  / ___// /_  ___  / / /  / ____/___ _____ ___  ___  _____
  \__ \/ __ \/ _ \/ / /  / / __/ __ `/ __ `__ \/ _ \/ ___/
 ___/ / / / /  __/ / /  / /_/ / /_/ / / / / / /  __(__  )
/____/_/ /_/\___/_/_/   \____/\__,_/_/ /_/ /_/\___/____/"""


# ---------------------------------------------------------------------------
# Widgets reusables
# ---------------------------------------------------------------------------


class Menu(Static, can_focus=True):
    """Lista de radios propia: icono/color igual al diseno (.tui), sin el
    estilo por defecto de Textual (circulo, fondo azul al enfocar)."""

    SELECTED_ICON = "▶"  # ▶
    UNSELECTED_ICON = "─"  # ─

    BINDINGS = [
        Binding("up", "cursor_up", "Arriba"),
        Binding("down", "cursor_down", "Abajo"),
        Binding("enter", "select", "Seleccionar", priority=True),
    ]

    index = reactive(0)

    class Selected(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(
        self,
        options: list[tuple[str, str]],
        show_hint: bool = True,
        breaks: set[int] = frozenset(),
        headers: dict[int, str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.options = options
        self.show_hint = show_hint
        self.breaks = breaks
        self.headers = headers or {}

    SELECT_DEBOUNCE = 0.15

    def on_mount(self) -> None:
        self._render_menu()
        self._focused_at = 0.0

    def focus(self, scroll_visible: bool = True) -> "Menu":
        # teclas Enter que ya estaban "en camino" (el usuario mashea Enter
        # justo cuando la pantalla anterior cambia, o cuando este menu
        # recien se activa tras terminar una partida) no deben colarse
        # aca y disparar una seleccion sin querer. Se resetea aca mismo
        # (no en un handler on_focus) para que quede garantizado antes de
        # que se procese cualquier tecla ya encolada.
        self._focused_at = time.monotonic()
        return super().focus(scroll_visible)

    def watch_index(self) -> None:
        self._render_menu()

    def _render_menu(self) -> None:
        lines = []
        for i, (_, label) in enumerate(self.options):
            if i in self.headers:
                if lines:
                    lines.append("")
                lines.append(f"[dim]{self.headers[i]}[/dim]")
            if i == self.index:
                lines.append(f"[green]{self.SELECTED_ICON} {label}[/green]")
            else:
                lines.append(f"[white]{self.UNSELECTED_ICON} {label}[/white]")
            if i in self.breaks:
                lines.append("")
        if self.show_hint:
            lines.append("")
            lines.append("[dim]\\[↑↓] mover   \\[Enter] seleccionar[/dim]")
        self.update("\n".join(lines))

    def action_cursor_up(self) -> None:
        self.index = (self.index - 1) % len(self.options)

    def action_cursor_down(self) -> None:
        self.index = (self.index + 1) % len(self.options)

    def action_select(self) -> None:
        if time.monotonic() - self._focused_at < self.SELECT_DEBOUNCE:
            return
        self.post_message(self.Selected(self.options[self.index][0]))


class Board(Static, can_focus=True):
    """Tablero 3x3 con separadores reales. Flechas+Enter o teclas 0-8 juegan."""

    SYMBOL_COLORS = {"X": "red", "O": "blue"}
    CURSOR_STYLE = "black on #90ee90"
    CURSOR_STYLE_WAIT = "white on grey23"
    WIN_COLOR = "black on green"
    LOSS_COLOR = "black on red"

    BINDINGS = [
        Binding("left", "cursor_left", "Izquierda"),
        Binding("right", "cursor_right", "Derecha"),
        Binding("up", "cursor_up", "Arriba"),
        Binding("down", "cursor_down", "Abajo"),
        Binding("enter", "select", "Jugar", priority=True),
    ] + [Binding(str(n), f"play({n})", f"Jugar {n}") for n in range(9)]

    cursor = reactive(0)

    class CellSelected(Message):
        def __init__(self, cell: int) -> None:
            self.cell = cell
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cells = ["."] * 9
        self.interactive = False
        self.finished = False
        self.my_symbol = "X"
        self.winning_line: tuple[int, int, int] | None = None
        self.own_win = False

    def on_mount(self) -> None:
        self._render_board()

    def watch_cursor(self) -> None:
        self._render_board()

    def set_state(self, board_str: str, interactive: bool, my_symbol: str) -> None:
        self.cells = list(board_str)
        self.interactive = interactive
        self.my_symbol = my_symbol
        self._render_board()

    def set_result(self, winning_line: tuple[int, int, int] | None, own_win: bool) -> None:
        self.interactive = False
        self.finished = True
        self.winning_line = winning_line
        self.own_win = own_win
        self._render_board()

    def _render_board(self) -> None:
        cells_text = []
        for i in range(9):
            c = self.cells[i]
            is_win_cell = bool(self.winning_line and i in self.winning_line)
            is_cursor = i == self.cursor and not is_win_cell and not self.finished

            if is_win_cell:
                bg = self.WIN_COLOR if self.own_win else self.LOSS_COLOR
                content = f"[{bg}] {c} [/{bg}]"
            elif is_cursor:
                shown = c if c in ("X", "O") else str(i)
                style = self.CURSOR_STYLE if self.interactive else self.CURSOR_STYLE_WAIT
                content = f"[{style}] {shown} [/{style}]"
            elif c in ("X", "O"):
                color = self.SYMBOL_COLORS.get(c, "white")
                content = f" [bold {color}]{c}[/bold {color}] "
            else:
                content = f" [dim]{i}[/dim] "
            cells_text.append(content)
        divider = "\n[dim]───┼───┼───[/dim]\n"
        rows = ["│".join(cells_text[r * 3:(r + 1) * 3]) for r in range(3)]
        self.update(divider.join(rows))

    def action_cursor_left(self) -> None:
        if not self.finished and self.cursor % 3 > 0:
            self.cursor -= 1

    def action_cursor_right(self) -> None:
        if not self.finished and self.cursor % 3 < 2:
            self.cursor += 1

    def action_cursor_up(self) -> None:
        if not self.finished and self.cursor >= 3:
            self.cursor -= 3

    def action_cursor_down(self) -> None:
        if not self.finished and self.cursor < 6:
            self.cursor += 3

    def action_select(self) -> None:
        self._try_play(self.cursor)

    def action_play(self, cell: int) -> None:
        self.cursor = cell
        self._try_play(cell)

    def _try_play(self, cell: int) -> None:
        if self.interactive and self.cells[cell] == ".":
            self.post_message(self.CellSelected(cell))


# ---------------------------------------------------------------------------
# Pantallas: identidad y estado
# ---------------------------------------------------------------------------


class NameScreen(Screen):
    """Primera pantalla: pide el nombre antes de conectar al servidor."""

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Ingresa tu nombre:", id="name-label")
            yield Input(placeholder="jugador", id="name-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # sanea igual que el servidor: el protocolo es texto separado por
        # espacios, un nombre con espacios rompe el parseo de !start/etc.
        name = re.sub(r"\s+", "_", event.value.strip())[:20]
        name = name or f"invitado{random.randint(100, 999)}"
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        app.begin_connection(name)


class MainScreen(Screen):
    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("", id="conn-status")
            yield Menu(
                [
                    ("play", "Jugar"),
                    ("about", "About"),
                    ("rename", "Cambiar nombre"),
                    ("quit", "Quit"),
                ],
                breaks={0},
                id="menu",
            )

    def on_mount(self) -> None:
        self.query_one(Menu).display = False
        self._refresh_conn_status()
        self.set_interval(0.5, self._refresh_conn_status)

    def _refresh_conn_status(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        status = self.query_one("#conn-status", Static)
        menu = self.query_one(Menu)
        if app.connected:
            status.update(f"[green]● conectado como {app.username}[/green]")
            if not menu.display:
                menu.display = True
                menu.focus()
        else:
            status.update("[yellow]◌ conectando...[/yellow]")
            menu.display = False

    def on_menu_selected(self, event: Menu.Selected) -> None:
        if event.value == "about":
            self.app.push_screen(AboutScreen())
        elif event.value == "play":
            self.app.push_screen(PlayScreen())
        elif event.value == "rename":
            self.app.push_screen(RenameScreen())
        elif event.value == "quit":
            self.app.action_quit_clean()  # type: ignore[attr-defined]


class RenameScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Volver")]
    RENAME_TIMEOUT = 8

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Nuevo nombre:", id="name-label")
            yield Input(placeholder="jugador", id="rename-input")
            yield Static("", id="invite-status")

    def on_mount(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        input_widget = self.query_one(Input)
        input_widget.value = app.username or ""
        input_widget.focus()
        self._wait_task: asyncio.Task | None = None

    def action_back(self) -> None:
        if self._wait_task:
            self._wait_task.cancel()
        self.app.pop_screen()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        name = re.sub(r"\s+", "_", event.value.strip())[:20]
        status = self.query_one("#invite-status", Static)
        if not name:
            status.update("[red]Escribe un nombre[/red]")
            return
        input_widget = self.query_one(Input)
        input_widget.disabled = True
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if not app.connected:
            status.update("[dim]Conectando al servidor...[/dim]")
        if not await app.wait_connected():
            status.update("[red]No se pudo conectar al servidor. Intenta de nuevo.[/red]")
            input_widget.disabled = False
            return
        status.update("[dim]Cambiando nombre... (Esc cancela)[/dim]")
        await app.send_line(f"/rename {name}")
        self._wait_task = asyncio.create_task(self._await_rename(status, input_widget))

    async def _await_rename(self, status: Static, input_widget: Input) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        try:
            new_name = await asyncio.wait_for(app.rename_queue.get(), timeout=self.RENAME_TIMEOUT)
        except asyncio.TimeoutError:
            status.update("[red]El servidor no respondio. Intenta de nuevo.[/red]")
            input_widget.disabled = False
            return
        app.username = new_name
        self.app.pop_screen()


class AboutScreen(Screen):
    """Misma estructura que MainScreen: banner arriba, info abajo, y un
    Menu de una opcion (Volver) para regresar."""

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Consultando servidor...", id="about-body")
            yield Menu([("back", "Volver")], id="menu")

    async def on_mount(self) -> None:
        self.query_one(Menu).focus()
        body = self.query_one("#about-body", Static)
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if not app.connected:
            body.update(f"[red]Desconectado[/red] de {app.url}")
            return
        who = await app.request_who()
        body.update(f"[green]Conectado[/green] a {app.url}\n\n{who}")

    def on_menu_selected(self, event: Menu.Selected) -> None:
        if event.value == "back":
            self.app.pop_screen()


# ---------------------------------------------------------------------------
# Pantallas: 3 en raya
# ---------------------------------------------------------------------------


class PlayScreen(Screen):
    """Unirme con codigo de una, o elegir que juego crear."""

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Menu(
                [
                    ("code_join", "Unirme con codigo"),
                    ("ttt", "3 en raya"),
                    ("hangman", "Ahorcado"),
                    ("battleship", "Batalla naval"),
                    ("back", "Volver"),
                ],
                breaks={0, 3},
                headers={1: "Crear una partida"},
                id="menu",
            )

    def on_mount(self) -> None:
        self.query_one(Menu).focus()

    def on_menu_selected(self, event: Menu.Selected) -> None:
        if event.value == "back":
            self.app.pop_screen()
        elif event.value == "code_join":
            self.app.push_screen(CodeJoinScreen())
        elif event.value in ("hangman", "battleship"):
            self.app.push_screen(ConfigScreen(event.value))
        else:
            self.app.push_screen(CreateMethodScreen(event.value))


HANGMAN_MODE_LABELS = {
    "classic": "Clasico (uno pone la palabra, el otro adivina)",
    "both": "Los dos adivinan (palabra al azar, gana quien falle menos)",
}
BATTLESHIP_PRESET_LABELS = {
    "classic": "Clasica (barcos 4, 3, 2)",
    "fast": "Rapida (barcos 3, 2)",
    "big": "Grande (barcos 5, 4, 3, 2)",
}


class ConfigScreen(Screen):
    """Elegir el modo/preset de la partida antes de crearla."""

    BINDINGS = [Binding("escape", "back", "Volver")]

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind

    def compose(self) -> ComposeResult:
        if self.kind == "hangman":
            labels = HANGMAN_MODE_LABELS
            options_order = HANGMAN_MODES
        else:
            labels = BATTLESHIP_PRESET_LABELS
            options_order = tuple(BATTLESHIP_PRESETS)
        options = [(value, labels[value]) for value in options_order]
        options.append(("back", "Volver"))
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Menu(options, breaks={len(options) - 2}, id="menu")

    def on_mount(self) -> None:
        self.query_one(Menu).focus()

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_menu_selected(self, event: Menu.Selected) -> None:
        if event.value == "back":
            self.app.pop_screen()
        else:
            self.app.push_screen(CreateMethodScreen(self.kind, event.value))


class CreateMethodScreen(Screen):
    def __init__(self, kind: str, config: str = "-") -> None:
        super().__init__()
        self.kind = kind
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Menu(
                [
                    ("byname", "Invitar por nombre"),
                    ("code_host", "Crear codigo"),
                    ("back", "Volver"),
                ],
                id="menu",
            )

    def on_mount(self) -> None:
        self.query_one(Menu).focus()

    def on_menu_selected(self, event: Menu.Selected) -> None:
        if event.value == "byname":
            self.app.push_screen(NameInviteScreen(self.kind, self.config))
        elif event.value == "code_host":
            self.app.push_screen(CodeHostScreen(self.kind, self.config))
        elif event.value == "back":
            self.app.pop_screen()


class NameInviteScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Volver")]

    def __init__(self, kind: str, config: str = "-") -> None:
        super().__init__()
        self.kind = kind
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Invita a alguien por su nombre:", id="name-label")
            yield Input(placeholder="nombre del jugador", id="invite-input")
            yield Static("", id="invite-status")

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._wait_task: asyncio.Task | None = None

    def action_back(self) -> None:
        if self._wait_task:
            self._wait_task.cancel()
        self.app.pop_screen()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        target = event.value.strip()
        status = self.query_one("#invite-status", Static)
        if not target:
            status.update("[red]Escribe un nombre[/red]")
            return
        input_widget = self.query_one(Input)
        input_widget.disabled = True
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if not app.connected:
            status.update("[dim]Conectando al servidor...[/dim]")
        if not await app.wait_connected():
            status.update("[red]No se pudo conectar al servidor. Intenta de nuevo.[/red]")
            input_widget.disabled = False
            return
        status.update(f"[dim]Invitando a {target}... esperando respuesta (Esc cancela)[/dim]")
        await app.send_line(f"/play {target} {self.kind} {self.config}")
        self._wait_task = asyncio.create_task(self._await_error(status, input_widget))

    async def _await_error(self, status: Static, input_widget: Input) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        error = await app.error_queue.get()
        status.update(f"[red]{error}[/red]")
        input_widget.disabled = False
        input_widget.value = ""
        input_widget.focus()


class CodeHostScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Volver")]

    def __init__(self, kind: str, config: str = "-") -> None:
        super().__init__()
        self.kind = kind
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Conectando...", id="code-status")

    async def on_mount(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        status = self.query_one("#code-status", Static)
        if not app.connected:
            status.update("[dim]Conectando al servidor...[/dim]")
        if not await app.wait_connected():
            status.update("[red]No se pudo conectar al servidor. Esc para volver.[/red]")
            return
        status.update("Generando codigo...")
        await app.send_line(f"/code {self.kind} {self.config}")
        gid, code = await app.code_queue.get()
        self.gid = gid
        status = self.query_one("#code-status", Static)
        status.update(
            f"Comparte este codigo:\n\n[green bold]{code}[/green bold]\n\n"
            "[dim]Esperando a que alguien se una... (Esc cancela)[/dim]"
        )

    def action_back(self) -> None:
        self.app.pop_screen()


class CodeJoinScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Volver")]

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Codigo de la partida:", id="name-label")
            yield Input(placeholder="XXXX", id="code-input")
            yield Static("", id="invite-status")

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._wait_task: asyncio.Task | None = None

    def action_back(self) -> None:
        if self._wait_task:
            self._wait_task.cancel()
        self.app.pop_screen()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        code = event.value.strip().upper()
        status = self.query_one("#invite-status", Static)
        if not code:
            status.update("[red]Escribe un codigo[/red]")
            return
        input_widget = self.query_one(Input)
        input_widget.disabled = True
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if not app.connected:
            status.update("[dim]Conectando al servidor...[/dim]")
        if not await app.wait_connected():
            status.update("[red]No se pudo conectar al servidor. Intenta de nuevo.[/red]")
            input_widget.disabled = False
            return
        status.update("[dim]Uniendote...[/dim]")
        await app.send_line(f"/join {code}")
        self._wait_task = asyncio.create_task(self._await_error(status, input_widget))

    async def _await_error(self, status: Static, input_widget: Input) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        error = await app.error_queue.get()
        status.update(f"[red]{error}[/red]")
        input_widget.disabled = False
        input_widget.value = ""
        input_widget.focus()


class InvitePopup(ModalScreen):
    """Notificacion de invitacion entrante, en cualquier pantalla."""

    BINDINGS = [
        Binding("enter", "accept", "Unirte", priority=True),
        Binding("escape", "decline", "Rechazar", priority=True),
    ]

    def __init__(self, gid: str, inviter: str, kind: str) -> None:
        super().__init__()
        self.gid = gid
        self.inviter = inviter
        self.kind = kind

    def compose(self) -> ComposeResult:
        game_name = GAME_NAMES.get(self.kind, self.kind)
        with Vertical(id="invite-box"):
            yield Static(f"[bold]{self.inviter} te invito a jugar: {game_name}[/bold]", id="invite-title")
            yield Static("[dim]Enter para unirte  ·  Esc para rechazar[/dim]", id="invite-hint")

    async def action_accept(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        await app.send_line(f"/accept {self.gid}")

    async def action_decline(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        await app.send_line(f"/decline {self.gid}")
        self.dismiss()


class AbandonConfirmScreen(ModalScreen):
    """Popup de confirmacion antes de abandonar una partida en curso."""

    def __init__(self, gid: str) -> None:
        super().__init__()
        self.gid = gid

    def compose(self) -> ComposeResult:
        with Vertical(id="invite-box"):
            yield Static("[bold]¿Estas seguro de abandonar?[/bold]", id="invite-title")
            yield Menu(
                [("no", "No, seguir jugando"), ("yes", "Si, abandonar")],
                show_hint=False,
                id="menu",
            )

    def on_mount(self) -> None:
        self.query_one(Menu).focus()

    def on_menu_selected(self, event: Menu.Selected) -> None:
        gid = self.gid
        self.dismiss()
        if event.value == "yes":
            app: "ShellGamesApp" = self.app  # type: ignore[assignment]
            app.run_worker(app.send_line(f"/forfeit {gid}"))


class FinishableScreen(Screen):
    """Comun a las 3 pantallas de juego: mientras se juega solo se ve
    "Volver [q]" (abandonar con confirmacion); al terminar se oculta eso y
    aparece el Menu "Volver" con flechita. Si nadie vuelve al menu en un
    minuto, vuelve solo."""

    AUTO_RETURN_SECONDS = 60

    def _finish_common(self) -> None:
        self.finished = True
        self.query_one("#quit-hint", Static).display = False
        menu = self.query_one(Menu)
        menu.display = True
        menu.focus()
        self.set_timer(self.AUTO_RETURN_SECONDS, self._auto_return)

    def _auto_return(self) -> None:
        if self.app.screen is self:
            self.app.pop_to_main()  # type: ignore[attr-defined]

    def on_menu_selected(self, event: Menu.Selected) -> None:
        if event.value == "back":
            self.app.pop_to_main()  # type: ignore[attr-defined]


class GameScreen(FinishableScreen):
    BINDINGS = [Binding("q", "confirm_abandon", "Abandonar")]

    def __init__(self, gid: str, opponent: str, symbol: str) -> None:
        super().__init__()
        self.gid = gid
        self.opponent = opponent
        self.symbol = symbol
        self.turn = "X"
        self.finished = False

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static("3 EN RAYA", id="game-title")
            yield Static(f"vs {self.opponent}", id="game-opponent")
            yield Board(id="board")
            yield Static("", id="game-status")
            yield Static("[dim]Volver \\[q][/dim]", id="quit-hint")
            yield Menu([("back", "Volver")], show_hint=False, id="menu")

    def on_mount(self) -> None:
        self.query_one(Menu).display = False
        self.apply_board("." * 9, "X")
        self.query_one(Board).focus()

    def action_confirm_abandon(self) -> None:
        if not self.finished:
            self.app.push_screen(AbandonConfirmScreen(self.gid))

    def _update_status(self) -> None:
        status = self.query_one("#game-status", Static)
        if self.finished:
            return
        if self.turn == self.symbol:
            status.update("[green]Tu turno[/green]")
        else:
            status.update(f"[dim]Turno de {self.opponent}[/dim]")

    def apply_board(self, board_str: str, turn: str) -> None:
        self.turn = turn
        board = self.query_one(Board)
        board.set_state(board_str, interactive=(turn == self.symbol and not self.finished), my_symbol=self.symbol)
        self._update_status()

    def _winning_line(self, board: Board) -> tuple[int, int, int] | None:
        for a, b, c in WIN_LINES:
            if board.cells[a] != "." and board.cells[a] == board.cells[b] == board.cells[c]:
                return (a, b, c)
        return None

    def _finish(self) -> None:
        self._finish_common()

    def apply_over(self, result: str, reason: str = "normal") -> None:
        board = self.query_one(Board)
        won = result == self.symbol
        line = self._winning_line(board)
        self._finish()
        board.set_result(line, own_win=won)
        status = self.query_one("#game-status", Static)
        if result == "draw":
            status.update("[yellow bold]Empate[/yellow bold]")
        elif reason == "timeout":
            if won:
                status.update(f"[green bold]Ganaste[/green bold] — {self.opponent} quedo inactivo")
            else:
                status.update("[red bold]Perdiste por inactividad[/red bold]")
        elif reason == "forfeit":
            if won:
                status.update(f"[green bold]Ganaste[/green bold] — {self.opponent} abandono")
            else:
                status.update("[yellow bold]Abandonaste la partida[/yellow bold]")
        elif won:
            status.update("[green bold]Ganaste![/green bold]")
        else:
            status.update(f"[red bold]Perdiste[/red bold] — gano {self.opponent}")

    def apply_opponent_left(self) -> None:
        self._finish()
        self.query_one(Board).set_result(None, False)
        status = self.query_one("#game-status", Static)
        status.update(f"[red]{self.opponent} se desconecto[/red]")

    def on_board_cell_selected(self, event: Board.CellSelected) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        self.run_worker(app.send_line(f"/move {self.gid} {event.cell}"))


# ---------------------------------------------------------------------------
# Pantallas: ahorcado
# ---------------------------------------------------------------------------


class HangmanScreen(FinishableScreen):
    BINDINGS = [Binding("q", "confirm_abandon", "Abandonar")]

    def __init__(self, gid: str, opponent: str, role: str, mode: str = "classic") -> None:
        super().__init__()
        self.gid = gid
        self.opponent = opponent
        self.role = role  # "setter"/"guesser" (classic) o "P1"/"P2" (both)
        self.mode = mode
        self.finished = False
        self.word_set = mode == "both"
        self._error_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static("AHORCADO", id="game-title")
            yield Static(f"vs {self.opponent}", id="game-opponent")
            yield Static("", id="hm-word")
            yield Static("", id="hm-misses")
            yield Static("", id="game-status")
            yield Input(id="hm-input")
            yield Static("[dim]Volver \\[q][/dim]", id="quit-hint")
            yield Menu([("back", "Volver")], show_hint=False, id="menu")

    def on_mount(self) -> None:
        self.query_one(Menu).display = False
        input_widget = self.query_one("#hm-input", Input)
        if self.mode == "both":
            input_widget.placeholder = "una letra"
            self.query_one("#hm-word", Static).update("[dim]Adivina la palabra a tu propio ritmo[/dim]")
            self.query_one("#game-status", Static).update("[green]Adivina una letra[/green]")
            input_widget.focus()
        elif self.role == "setter":
            input_widget.placeholder = "escribe la palabra secreta"
            self.query_one("#hm-word", Static).update("Elegi una palabra para que tu rival adivine")
            input_widget.focus()
        else:
            input_widget.placeholder = "una letra"
            input_widget.disabled = True
            self.query_one("#hm-word", Static).update("[dim]Esperando que el rival elija la palabra...[/dim]")
        self._error_task = asyncio.create_task(self._watch_errors())

    async def _watch_errors(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        while True:
            error = await app.error_queue.get()
            if not self.finished:
                self.query_one("#game-status", Static).update(f"[red]{error}[/red]")

    def action_confirm_abandon(self) -> None:
        if not self.finished:
            self.app.push_screen(AbandonConfirmScreen(self.gid))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip().lower()
        input_widget = self.query_one("#hm-input", Input)
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if self.mode != "both" and self.role == "setter" and not self.word_set:
            if not value.isalpha() or not (3 <= len(value) <= 20):
                self.query_one("#game-status", Static).update("[red]Solo letras, entre 3 y 20[/red]")
                return
            await app.send_line(f"/hangword {self.gid} {value}")
            input_widget.value = ""
        elif (self.mode == "both" or self.role == "guesser") and self.word_set and not self.finished:
            letter = value[:1]
            if not letter.isalpha():
                self.query_one("#game-status", Static).update("[red]Escribe una letra[/red]")
                return
            await app.send_line(f"/guess {self.gid} {letter}")
            input_widget.value = ""

    def apply_state(self, revealed: str, misses: int) -> None:
        self.word_set = True
        self.query_one("#hm-word", Static).update(f"[bold]{' '.join(revealed.upper())}[/bold]")
        self.query_one("#hm-misses", Static).update(f"Fallos: {misses}/{HANGMAN_MAX_MISSES}")
        input_widget = self.query_one("#hm-input", Input)
        status = self.query_one("#game-status", Static)
        if self.finished:
            return
        if self.mode == "both":
            input_widget.disabled = False
            status.update("[green]Adivina una letra[/green]")
            input_widget.focus()
        elif self.role == "setter":
            input_widget.disabled = True
            status.update(f"[dim]Esperando que {self.opponent} adivine...[/dim]")
        else:
            input_widget.disabled = False
            status.update("[green]Adivina una letra[/green]")
            input_widget.focus()

    def _finish(self) -> None:
        if self._error_task:
            self._error_task.cancel()
        self.query_one("#hm-input", Input).disabled = True
        self._finish_common()

    def apply_over(self, result: str, reason: str = "normal") -> None:
        self._finish()
        won = result == self.role
        status = self.query_one("#game-status", Static)
        if result == "draw":
            status.update("[yellow bold]Empate[/yellow bold]")
        elif reason == "timeout":
            if won:
                status.update(f"[green bold]Ganaste[/green bold] — {self.opponent} quedo inactivo")
            else:
                status.update("[red bold]Perdiste por inactividad[/red bold]")
        elif reason == "forfeit":
            if won:
                status.update(f"[green bold]Ganaste[/green bold] — {self.opponent} abandono")
            else:
                status.update("[yellow bold]Abandonaste la partida[/yellow bold]")
        elif won:
            status.update("[green bold]Ganaste![/green bold]")
        else:
            status.update("[red bold]Perdiste[/red bold]")

    def apply_opponent_left(self) -> None:
        self._finish()
        self.query_one("#game-status", Static).update(f"[red]{self.opponent} se desconecto[/red]")


# ---------------------------------------------------------------------------
# Pantallas: batalla naval
# ---------------------------------------------------------------------------


class BSBoard(Static, can_focus=True):
    """Tablero 8x8 de batalla naval, navegable con flechas."""

    CURSOR_STYLE = "black on #90ee90"

    BINDINGS = [
        Binding("left", "cursor_left", "Izquierda"),
        Binding("right", "cursor_right", "Derecha"),
        Binding("up", "cursor_up", "Arriba"),
        Binding("down", "cursor_down", "Abajo"),
        Binding("enter", "select", "Confirmar", priority=True),
        Binding("r", "rotate", "Rotar"),
    ]

    cursor = reactive(0)

    class CellSelected(Message):
        def __init__(self, row: int, col: int) -> None:
            self.row = row
            self.col = col
            super().__init__()

    def __init__(self, compact: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cells = ["."] * (BOARD_SIZE * BOARD_SIZE)
        self.interactive = False
        self.show_own = True  # True: tablero propio (con barcos); False: seguimiento del rival
        self.ghost_len = 0  # largo del barco a previsualizar (fase de colocacion)
        self.orientation = "h"
        self.compact = compact  # tablero mini (mi flota): sin espacios entre celdas

    def on_mount(self) -> None:
        self._render_board()

    def watch_cursor(self) -> None:
        self._render_board()

    def set_cells(self, cells_str: str, interactive: bool) -> None:
        self.cells = list(cells_str)
        self.interactive = interactive
        self._render_board()

    def action_rotate(self) -> None:
        self.orientation = "v" if self.orientation == "h" else "h"
        self._clamp_cursor()
        self._render_board()

    def set_ghost_len(self, length: int) -> None:
        self.ghost_len = length
        self._clamp_cursor()
        self._render_board()

    def _max_row(self) -> int:
        if self.ghost_len and self.orientation == "v":
            return BOARD_SIZE - self.ghost_len
        return BOARD_SIZE - 1

    def _max_col(self) -> int:
        if self.ghost_len and self.orientation == "h":
            return BOARD_SIZE - self.ghost_len
        return BOARD_SIZE - 1

    def _clamp_cursor(self) -> None:
        """El barco que se esta colocando nunca debe poder apuntar fuera
        del tablero: se limita por donde puede pararse el cursor segun su
        largo y orientacion actual."""
        row, col = divmod(self.cursor, BOARD_SIZE)
        row = min(row, self._max_row())
        col = min(col, self._max_col())
        self.cursor = row * BOARD_SIZE + col

    def _ghost_cells(self) -> set[int]:
        if not self.ghost_len:
            return set()
        row, col = divmod(self.cursor, BOARD_SIZE)
        out = set()
        for i in range(self.ghost_len):
            r = row + (i if self.orientation == "v" else 0)
            c = col + (i if self.orientation == "h" else 0)
            if 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE:
                out.add(r * BOARD_SIZE + c)
        return out

    def _render_board(self) -> None:
        ghost = self._ghost_cells()
        lines = []
        for row in range(BOARD_SIZE):
            cells_text = []
            for col in range(BOARD_SIZE):
                idx = row * BOARD_SIZE + col
                c = self.cells[idx]
                if c == "X":
                    hit_color = "red" if self.show_own else "green"
                    ch = f"[bold {hit_color}]X[/bold {hit_color}]"
                elif c == "o":
                    ch = "[dim]o[/dim]"
                elif c == "S" and self.show_own:
                    ch = "[bright_green]S[/bright_green]"
                else:
                    ch = "[blue]·[/blue]"
                if self.interactive and idx == self.cursor:
                    ch = f"[{self.CURSOR_STYLE}]{c if c in ('X','o') else '+'}[/{self.CURSOR_STYLE}]"
                elif self.interactive and idx in ghost:
                    ch = "[black on #90ee90]~[/black on #90ee90]"
                cells_text.append(ch)
            lines.append(("" if self.compact else " ").join(cells_text))
        self.update("\n".join(lines))

    def action_cursor_left(self) -> None:
        if self.interactive and self.cursor % BOARD_SIZE > 0:
            self.cursor -= 1

    def action_cursor_right(self) -> None:
        if self.interactive and self.cursor % BOARD_SIZE < self._max_col():
            self.cursor += 1

    def action_cursor_up(self) -> None:
        if self.interactive and self.cursor >= BOARD_SIZE:
            self.cursor -= BOARD_SIZE

    def action_cursor_down(self) -> None:
        row, _ = divmod(self.cursor, BOARD_SIZE)
        if self.interactive and row < self._max_row():
            self.cursor += BOARD_SIZE

    def action_select(self) -> None:
        if not self.interactive:
            return
        row, col = divmod(self.cursor, BOARD_SIZE)
        self.post_message(self.CellSelected(row, col))


class ControlsHelpScreen(ModalScreen):
    """Recordatorio de controles, mostrado una vez al entrar a la partida."""

    BINDINGS = [Binding("enter", "dismiss_help", "Cerrar", priority=True)]

    def compose(self) -> ComposeResult:
        with Vertical(id="invite-box"):
            yield Static("[bold]Controles[/bold]", id="invite-title")
            yield Static("[dim]Flechas: mover[/dim]", id="ch-1")
            yield Static("[dim]R: rotar el barco[/dim]", id="ch-2")
            yield Static("[dim]Enter: colocar / disparar[/dim]", id="ch-3")
            yield Static("[dim]Q: abandonar[/dim]", id="ch-4")
            yield Static("[dim]Enter para continuar[/dim]", id="invite-hint2")

    def action_dismiss_help(self) -> None:
        self.dismiss()


class BattleshipScreen(FinishableScreen):
    BINDINGS = [Binding("q", "confirm_abandon", "Abandonar")]

    def __init__(self, gid: str, opponent: str, symbol: str, preset: str = "classic") -> None:
        super().__init__()
        self.gid = gid
        self.opponent = opponent
        self.symbol = symbol
        self.ship_sizes = BATTLESHIP_PRESETS.get(preset, BATTLESHIP_PRESETS["classic"])
        self.finished = False
        self.phase = "placing"
        self.next_ship = 0
        self.remaining_sizes = list(self.ship_sizes)
        self._error_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static("BATALLA NAVAL", id="game-title")
            yield Static(f"vs {self.opponent}", id="game-opponent")
            yield Static("", id="game-status")
            yield Static("", id="fleet-label")
            yield BSBoard(compact=True, id="own-board")
            yield Static("", id="remaining-label")
            yield BSBoard(id="board")
            yield Static("[dim]Volver \\[q][/dim]", id="quit-hint")
            yield Menu([("back", "Volver")], show_hint=False, id="menu")

    def on_mount(self) -> None:
        self.query_one(Menu).display = False
        self.query_one("#fleet-label", Static).display = False
        self.query_one("#own-board", BSBoard).display = False
        self.query_one("#remaining-label", Static).display = False
        board = self.query_one("#board", BSBoard)
        board.interactive = True
        board.set_ghost_len(self.ship_sizes[0])
        board.focus()
        self._update_status()
        self._error_task = asyncio.create_task(self._watch_errors())
        self.app.push_screen(ControlsHelpScreen(), callback=lambda _: board.focus())

    async def _watch_errors(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        while True:
            error = await app.error_queue.get()
            if not self.finished:
                self.query_one("#game-status", Static).update(f"[red]{error}[/red]")

    def _update_status(self) -> None:
        if self.finished:
            return
        status = self.query_one("#game-status", Static)
        if self.phase == "placing":
            if self.next_ship < len(self.ship_sizes):
                size = self.ship_sizes[self.next_ship]
                status.update(f"Coloca tu barco de {size} ({self.next_ship + 1}/{len(self.ship_sizes)}) — [dim]r rota[/dim]")
            else:
                status.update("[dim]Esperando que termine el rival de colocar...[/dim]")

    def action_confirm_abandon(self) -> None:
        if not self.finished:
            self.app.push_screen(AbandonConfirmScreen(self.gid))

    def on_bsboard_cell_selected(self, event: BSBoard.CellSelected) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        board = self.query_one("#board", BSBoard)
        if self.phase == "placing":
            if self.next_ship >= len(self.ship_sizes):
                return
            self.run_worker(
                app.send_line(f"/place {self.gid} {self.next_ship} {event.row} {event.col} {board.orientation}")
            )
        else:
            self.run_worker(app.send_line(f"/shot {self.gid} {event.row} {event.col}"))

    def _ships_completed(self, own: str) -> int:
        """Cuenta cuantos barcos propios ya estan colocados, a partir del
        tablero que manda el servidor (fuente de verdad, no un contador local
        que podria desincronizarse con la red)."""
        placed_cells = own.count("S") + own.count("X")
        total = 0
        count = 0
        for size in self.ship_sizes:
            total += size
            if placed_cells >= total:
                count += 1
            else:
                break
        return count

    def _update_remaining_label(self) -> None:
        label = self.query_one("#remaining-label", Static)
        if self.remaining_sizes:
            sizes = ", ".join(str(s) for s in self.remaining_sizes)
            label.update(f"[dim]Barcos rivales por hundir: {sizes}[/dim]")
        else:
            label.update("[green]Encontraste toda la flota rival[/green]")

    def apply_state(
        self,
        own: str,
        tracking: str,
        phase: str,
        turn_symbol: str,
        shooter_symbol: str = "-",
        sunk_size: int | None = None,
    ) -> None:
        board = self.query_one("#board", BSBoard)
        if phase == "placing":
            self.next_ship = self._ships_completed(own)
            board.set_ghost_len(self.ship_sizes[self.next_ship] if self.next_ship < len(self.ship_sizes) else 0)
            board.show_own = True
            board.set_cells(own, interactive=self.next_ship < len(self.ship_sizes))
            self._update_status()
            return
        if self.phase != "battle":
            self.phase = "battle"
            board.show_own = False
            board.set_ghost_len(0)
            self.query_one("#fleet-label", Static).display = True
            self.query_one("#fleet-label", Static).update("[dim]Tu flota:[/dim]")
            self.query_one("#own-board", BSBoard).display = True
            self.query_one("#remaining-label", Static).display = True
            self._update_remaining_label()
        own_board = self.query_one("#own-board", BSBoard)
        own_board.show_own = True
        own_board.set_cells(own, interactive=False)
        if sunk_size is not None and shooter_symbol == self.symbol and sunk_size in self.remaining_sizes:
            self.remaining_sizes.remove(sunk_size)
            self._update_remaining_label()
        my_turn = turn_symbol == self.symbol
        board.set_cells(tracking, interactive=my_turn and not self.finished)
        status = self.query_one("#game-status", Static)
        if not self.finished:
            status.update("[green]Tu turno: elige donde disparar[/green]" if my_turn else f"[dim]Turno de {self.opponent}[/dim]")

    def _finish(self) -> None:
        if self._error_task:
            self._error_task.cancel()
        self.query_one("#board", BSBoard).interactive = False
        self._finish_common()

    def apply_over(self, result: str, reason: str = "normal") -> None:
        self._finish()
        won = result == self.symbol
        status = self.query_one("#game-status", Static)
        if reason == "timeout":
            if won:
                status.update(f"[green bold]Ganaste[/green bold] — {self.opponent} quedo inactivo")
            else:
                status.update("[red bold]Perdiste por inactividad[/red bold]")
        elif reason == "forfeit":
            if won:
                status.update(f"[green bold]Ganaste[/green bold] — {self.opponent} abandono")
            else:
                status.update("[yellow bold]Abandonaste la partida[/yellow bold]")
        elif won:
            status.update("[green bold]Hundiste toda la flota rival![/green bold]")
        else:
            status.update("[red bold]Tu flota fue hundida[/red bold]")

    def apply_opponent_left(self) -> None:
        self._finish()
        self.query_one("#game-status", Static).update(f"[red]{self.opponent} se desconecto[/red]")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class ShellGamesApp(App):
    CSS = """
    Screen { align: center middle; background: #0c0e14; }
    #main-box {
        width: auto; min-width: 64; height: auto; background: #0c0e14;
        border: round green; padding: 2 5;
    }
    #banner { color: green; text-style: bold; text-align: left; width: auto; margin-bottom: 1; }
    #conn-status { width: auto; margin-bottom: 1; }
    Menu { width: auto; height: auto; background: transparent; border: none; margin-top: 1; }
    Menu:focus { background: transparent; }
    #about-body { color: white; width: auto; margin-bottom: 1; }
    #name-label { color: white; width: auto; margin-bottom: 1; }
    #invite-status { color: white; width: auto; margin-top: 1; }
    #code-status { color: white; width: auto; }
    #game-title { color: green; text-style: bold; width: 100%; text-align: center; }
    #game-opponent { color: white; width: 100%; text-align: center; margin-bottom: 1; }
    #game-status { color: white; width: 100%; text-align: center; margin: 1 0; }
    #quit-hint { width: 100%; text-align: center; margin-top: 1; }
    Board { width: 100%; height: auto; background: transparent; margin-bottom: 1; content-align: center middle; }
    #hm-word { width: 100%; text-align: center; color: white; margin: 1 0; }
    #hm-misses { width: 100%; text-align: center; color: yellow; margin-bottom: 1; }
    BSBoard { width: 100%; height: auto; background: transparent; margin-bottom: 1; content-align: center middle; }
    #fleet-label { width: 100%; text-align: center; }
    #own-board { margin-bottom: 1; }
    #remaining-label { width: 100%; text-align: center; margin-bottom: 1; }
    Input { background: transparent; border: round green; width: 44; }
    Input:focus { border: round green; }
    InvitePopup { align: center middle; background: black 60%; }
    ControlsHelpScreen { align: center middle; background: black 60%; }
    #invite-box { width: auto; height: auto; border: round green; padding: 1 4; background: #14161f; }
    #invite-title { width: auto; margin-bottom: 1; }
    #invite-hint { width: auto; }
    #ch-1, #ch-2, #ch-3, #ch-4 { width: auto; }
    #ch-4 { margin-bottom: 1; }
    #invite-hint2 { width: auto; }
    """

    def __init__(self, url: str = DEFAULT_URL) -> None:
        super().__init__()
        self.url = url
        self.ws = None
        self.connected = False
        self.username: str | None = None
        self.connected_event = asyncio.Event()
        self._who_queue: "asyncio.Queue[str]" = asyncio.Queue()
        self.error_queue: "asyncio.Queue[str]" = asyncio.Queue()
        self.code_queue: "asyncio.Queue[tuple[str, str]]" = asyncio.Queue()
        self.rename_queue: "asyncio.Queue[str]" = asyncio.Queue()

    def on_mount(self) -> None:
        self.push_screen(NameScreen())

    def begin_connection(self, username: str) -> None:
        self.switch_screen(MainScreen())
        self.run_worker(self._connect_loop(username), exclusive=True)

    def pop_to_main(self) -> None:
        """Vuelve al menu principal, sin importar cuantas pantallas haya
        apiladas encima (invitar/codigo/juego)."""
        while len(self.screen_stack) > 1 and not isinstance(self.screen, MainScreen):
            self.pop_screen()
        if isinstance(self.screen, MainScreen):
            # reenfocar reinicia el "enfriamiento" del menu: Enters que
            # venian de mashear al terminar la partida anterior no deben
            # colarse aca y disparar una navegacion sin querer.
            menu = self.screen.query_one(Menu)
            if menu.display:
                menu.focus()

    async def send_line(self, text: str) -> None:
        if self.ws is not None:
            try:
                await self.ws.send(text)
            except Exception:
                pass

    async def wait_connected(self, timeout: float = 8) -> bool:
        """Espera a que la conexion este lista. Devuelve False si expira el timeout."""
        try:
            await asyncio.wait_for(self.connected_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _connect_loop(self, username: str) -> None:
        while True:
            try:
                async with connect(self.url, open_timeout=10) as ws:
                    self.ws = ws
                    await asyncio.wait_for(ws.recv(), timeout=10)  # "Nombre: "
                    # usar self.username (no el parametro original) para que
                    # una reconexion respete un /rename hecho antes del corte
                    username = self.username or username
                    self.username = username
                    await ws.send(username)
                    await asyncio.wait_for(ws.recv(), timeout=10)  # "Conectado como ..."
                    self.connected = True
                    self.connected_event.set()
                    async for raw in ws:
                        self._dispatch(raw.strip())
            except Exception:
                pass
            finally:
                self.connected = False
                self.connected_event.clear()
                self.ws = None
            await asyncio.sleep(2)  # reintenta la conexion

    def _dispatch(self, msg: str) -> None:
        game_screens = (GameScreen, HangmanScreen, BattleshipScreen)
        if msg.startswith("* conectados"):
            self._who_queue.put_nowait(msg)
        elif msg.startswith("!invite "):
            _, gid, inviter, kind, config = msg.split()
            if isinstance(self.screen, game_screens) and self.screen.finished:
                # no dejar la partida vieja terminada debajo en la pila: si
                # despues aceptan esta invitacion y "Volver" en la nueva
                # partida, debe ir al menu, no a la pantalla vieja.
                self.pop_to_main()
            self.push_screen(InvitePopup(gid, inviter, kind))
        elif msg.startswith("!start "):
            _, gid, opponent, symbol, kind, config = msg.split()
            if kind == "hangman":
                self.switch_screen(HangmanScreen(gid, opponent, symbol, config))
            elif kind == "battleship":
                self.switch_screen(BattleshipScreen(gid, opponent, symbol, config))
            else:
                self.switch_screen(GameScreen(gid, opponent, symbol))
        elif msg.startswith("!board "):
            _, gid, board, turn = msg.split()
            if isinstance(self.screen, GameScreen) and self.screen.gid == gid:
                self.screen.apply_board(board, turn)
        elif msg.startswith("!hm_state "):
            _, gid, revealed, misses = msg.split()
            if isinstance(self.screen, HangmanScreen) and self.screen.gid == gid:
                self.screen.apply_state(revealed, int(misses))
        elif msg.startswith("!bs_state "):
            _, gid, own, tracking, phase, turn_symbol, shooter_symbol, sunk = msg.split()
            if isinstance(self.screen, BattleshipScreen) and self.screen.gid == gid:
                sunk_size = int(sunk) if sunk != "-" else None
                self.screen.apply_state(own, tracking, phase, turn_symbol, shooter_symbol, sunk_size)
        elif msg.startswith("!over "):
            parts = msg.split()
            gid, result = parts[1], parts[2]
            reason = parts[3] if len(parts) > 3 else "normal"
            if isinstance(self.screen, game_screens) and self.screen.gid == gid:
                self.screen.apply_over(result, reason)
        elif msg.startswith("!opponent_left "):
            _, gid = msg.split()
            if isinstance(self.screen, game_screens) and self.screen.gid == gid:
                self.screen.apply_opponent_left()
        elif msg.startswith("!code "):
            _, gid, code = msg.split()
            self.code_queue.put_nowait((gid, code))
        elif msg.startswith("!renamed "):
            self.rename_queue.put_nowait(msg.split()[1])
        elif msg.startswith("!error "):
            self.error_queue.put_nowait(msg[len("!error "):])
        elif msg.startswith("!declined "):
            _, gid, who = msg.split()
            self.error_queue.put_nowait(f"{who} rechazo la invitacion")
        elif msg.startswith("!invited "):
            pass  # confirmacion silenciosa

    async def request_who(self) -> str:
        if not self.connected or self.ws is None:
            return "Desconectado del servidor."
        await self.ws.send("/who")
        try:
            return await asyncio.wait_for(self._who_queue.get(), timeout=5)
        except asyncio.TimeoutError:
            return "Sin respuesta del servidor."

    def action_quit_clean(self) -> None:
        async def _quit() -> None:
            if self.ws is not None:
                try:
                    await self.ws.send("/quit")
                except Exception:
                    pass
            self.exit()

        self.run_worker(_quit())


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    ShellGamesApp(url).run()
    os.system("clear")
