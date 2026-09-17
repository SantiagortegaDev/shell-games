#!/usr/bin/env python3
"""TUI client for Shell Games (Textual): menus, chat status and tic-tac-toe."""
import asyncio
import os
import random
import re
import sys
import time

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, Static
from websockets.asyncio.client import connect

from games import BATTLESHIP_PRESETS, BOARD_SIZE, HANGMAN_MAX_MISSES, HANGMAN_MODES, WIN_LINES

DEFAULT_URL = "wss://shellgames.santiagortega.dev"

GAME_NAMES = {"ttt": "Tic-tac-toe", "hangman": "Hangman", "battleship": "Battleship"}


def parse_match_config(config: str) -> tuple[str, int, int, int, int]:
    """Splits 'base:rounds:host_wins:guest_wins:round_num' into parts.
    No suffix means a single-round match (default)."""
    parts = config.split(":")
    base = parts[0]
    if len(parts) == 5:
        try:
            return base, int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            pass
    return base, 1, 0, 0, 1


HOST_SYMBOLS = {"ttt": "X", "battleship": "P1"}


def is_host_symbol(kind: str, symbol: str) -> bool:
    if kind == "hangman":
        return symbol in ("setter", "P1")
    return symbol == HOST_SYMBOLS.get(kind, symbol)

BANNER = r"""   _____ __         ____   ______
  / ___// /_  ___  / / /  / ____/___ _____ ___  ___  _____
  \__ \/ __ \/ _ \/ / /  / / __/ __ `/ __ `__ \/ _ \/ ___/
 ___/ / / / /  __/ / /  / /_/ / /_/ / / / / / /  __(__  )
/____/_/ /_/\___/_/_/   \____/\__,_/_/ /_/ /_/\___/____/"""


# ---------------------------------------------------------------------------
# Reusable widgets
# ---------------------------------------------------------------------------


class Menu(Static, can_focus=True):
    """Custom radio list: icon/color matches the design (.tui), without
    Textual's default styling (circle, blue background on focus)."""

    SELECTED_ICON = "▶"  # ▶
    UNSELECTED_ICON = "─"  # ─

    BINDINGS = [
        Binding("up", "cursor_up", "Up"),
        Binding("down", "cursor_down", "Down"),
        Binding("enter", "select", "Select", priority=True),
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
        # Enter keys already "in flight" (the user mashing Enter right as
        # the previous screen changes, or right when this menu becomes
        # active after a game ends) must not leak in here and trigger an
        # accidental selection. Reset happens right here (not in an
        # on_focus handler) so it's guaranteed before any already-queued
        # key gets processed.
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
            lines.append("[dim]\\[↑↓] move   \\[Enter] select[/dim]")
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
    """3x3 board with real dividers. Arrows+Enter or keys 0-8 play."""

    SYMBOL_COLORS = {"X": "red", "O": "blue"}
    CURSOR_STYLE = "black on #90ee90"
    CURSOR_STYLE_WAIT = "white on grey23"
    WIN_COLOR = "black on green"
    LOSS_COLOR = "black on red"

    BINDINGS = [
        Binding("left", "cursor_left", "Left"),
        Binding("right", "cursor_right", "Right"),
        Binding("up", "cursor_up", "Up"),
        Binding("down", "cursor_down", "Down"),
        Binding("enter", "select", "Play", priority=True),
    ] + [Binding(str(n), f"play({n})", f"Play {n}") for n in range(9)]

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
            elif c in ("X", "O"):
                color = self.SYMBOL_COLORS.get(c, "white")
                content = f" [bold {color}]{c}[/bold {color}] "
            elif is_cursor:
                style = self.CURSOR_STYLE if self.interactive else self.CURSOR_STYLE_WAIT
                content = f"[{style}] {i} [/{style}]"
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
# Screens: identity and status
# ---------------------------------------------------------------------------


class NameScreen(Screen):
    """First screen: asks for a name before connecting to the server."""

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Enter your name:", id="name-label")
            yield Input(placeholder="player", id="name-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # sanitized the same way as the server: the protocol is
        # space-separated text, a name with spaces would break
        # !start/etc. parsing.
        name = re.sub(r"\s+", "_", event.value.strip())[:20]
        name = name or f"guest{random.randint(100, 999)}"
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        app.begin_connection(name)


class MainScreen(Screen):
    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("", id="conn-status")
            yield Menu(
                [
                    ("play", "Play"),
                    ("about", "About"),
                    ("rename", "Change name"),
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
            status.update(f"[green]● connected as {app.username}[/green]")
            if not menu.display:
                menu.display = True
                menu.focus()
        else:
            status.update("[yellow]◌ connecting...[/yellow]")
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
    BINDINGS = [Binding("escape", "back", "Back")]
    RENAME_TIMEOUT = 8

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("New name:", id="name-label")
            yield Input(placeholder="player", id="rename-input")
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
            status.update("[red]Type a name[/red]")
            return
        input_widget = self.query_one(Input)
        input_widget.disabled = True
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if not app.connected:
            status.update("[dim]Connecting to server...[/dim]")
        if not await app.wait_connected():
            status.update("[red]Couldn't connect to the server. Try again.[/red]")
            input_widget.disabled = False
            return
        status.update("[dim]Changing name... (Esc cancels)[/dim]")
        await app.send_line(f"/rename {name}")
        self._wait_task = asyncio.create_task(self._await_rename(status, input_widget))

    async def _await_rename(self, status: Static, input_widget: Input) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        try:
            new_name = await asyncio.wait_for(app.rename_queue.get(), timeout=self.RENAME_TIMEOUT)
        except asyncio.TimeoutError:
            status.update("[red]The server didn't respond. Try again.[/red]")
            input_widget.disabled = False
            return
        app.username = new_name
        self.app.pop_screen()


class AboutScreen(Screen):
    """Same structure as MainScreen: banner on top, info below, and a
    single-option Menu (Back) to return."""

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Checking server...", id="about-body")
            yield Menu([("back", "Back")], id="menu")

    async def on_mount(self) -> None:
        self.query_one(Menu).focus()
        body = self.query_one("#about-body", Static)
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if not app.connected:
            body.update(f"[red]Disconnected[/red] from {app.url}")
            return
        who = await app.request_who()
        body.update(f"[green]Connected[/green] to {app.url}\n\n{who}")

    def on_menu_selected(self, event: Menu.Selected) -> None:
        if event.value == "back":
            self.app.pop_screen()


# ---------------------------------------------------------------------------
# Screens: tic-tac-toe
# ---------------------------------------------------------------------------


class PlayScreen(Screen):
    """Join with a code right away, or choose which game to create."""

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Menu(
                [
                    ("code_join", "Join with a code"),
                    ("ttt", "Tic-tac-toe"),
                    ("hangman", "Hangman"),
                    ("battleship", "Battleship"),
                    ("back", "Back"),
                ],
                breaks={0, 3},
                headers={1: "Create a game"},
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
            self.app.push_screen(RoundsScreen(event.value, "-"))


HANGMAN_MODE_LABELS = {
    "classic": "Classic (one sets the word, the other guesses)",
    "race": "Race (random word, first to guess it all wins)",
    "turns": "Turns (shared word, a hit earns another turn)",
}
BATTLESHIP_PRESET_LABELS = {
    "classic": "Classic (ships 4, 3, 2)",
    "fast": "Fast (ships 3, 2)",
    "big": "Big (ships 5, 4, 3, 2)",
}


class ConfigScreen(Screen):
    """Choose the game's mode/preset before creating it."""

    BINDINGS = [Binding("escape", "back", "Back")]

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
        options.append(("back", "Back"))
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
            self.app.push_screen(RoundsScreen(self.kind, event.value))


class RoundsScreen(Screen):
    """Choose how many rounds the match has (best-of-N)."""

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, kind: str, base_config: str) -> None:
        super().__init__()
        self.kind = kind
        self.base_config = base_config

    def compose(self) -> ComposeResult:
        options = [
            ("1", "Single game"),
            ("3", "Best of 3"),
            ("5", "Best of 5"),
            ("back", "Back"),
        ]
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
            return
        rounds = int(event.value)
        config = self.base_config if rounds <= 1 else f"{self.base_config}:{rounds}:0:0:1"
        self.app.push_screen(CreateMethodScreen(self.kind, config))


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
                    ("byname", "Invite by name"),
                    ("code_host", "Create a code"),
                    ("back", "Back"),
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
    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, kind: str, config: str = "-") -> None:
        super().__init__()
        self.kind = kind
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Invite someone by their name:", id="name-label")
            yield Input(placeholder="player's name", id="invite-input")
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
            status.update("[red]Type a name[/red]")
            return
        input_widget = self.query_one(Input)
        input_widget.disabled = True
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if not app.connected:
            status.update("[dim]Connecting to server...[/dim]")
        if not await app.wait_connected():
            status.update("[red]Couldn't connect to the server. Try again.[/red]")
            input_widget.disabled = False
            return
        status.update(f"[dim]Inviting {target}... waiting for a response (Esc cancels)[/dim]")
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
    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, kind: str, config: str = "-") -> None:
        super().__init__()
        self.kind = kind
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Connecting...", id="code-status")

    async def on_mount(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        status = self.query_one("#code-status", Static)
        if not app.connected:
            status.update("[dim]Connecting to server...[/dim]")
        if not await app.wait_connected():
            status.update("[red]Couldn't connect to the server. Esc to go back.[/red]")
            return
        status.update("Generating code...")
        await app.send_line(f"/code {self.kind} {self.config}")
        gid, code = await app.code_queue.get()
        self.gid = gid
        status = self.query_one("#code-status", Static)
        status.update(
            f"Share this code:\n\n[green bold]{code}[/green bold]\n\n"
            "[dim]Waiting for someone to join... (Esc cancels)[/dim]"
        )

    def action_back(self) -> None:
        self.app.pop_screen()


class CodeJoinScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static(BANNER, id="banner")
            yield Static("Game code:", id="name-label")
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
            status.update("[red]Type a code[/red]")
            return
        input_widget = self.query_one(Input)
        input_widget.disabled = True
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if not app.connected:
            status.update("[dim]Connecting to server...[/dim]")
        if not await app.wait_connected():
            status.update("[red]Couldn't connect to the server. Try again.[/red]")
            input_widget.disabled = False
            return
        status.update("[dim]Joining...[/dim]")
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
    """Incoming invitation notification, from any screen."""

    BINDINGS = [
        Binding("enter", "accept", "Join", priority=True),
        Binding("escape", "decline", "Decline", priority=True),
    ]

    def __init__(self, gid: str, inviter: str, kind: str) -> None:
        super().__init__()
        self.gid = gid
        self.inviter = inviter
        self.kind = kind

    def compose(self) -> ComposeResult:
        game_name = GAME_NAMES.get(self.kind, self.kind)
        with Vertical(id="invite-box"):
            yield Static(f"[bold]{self.inviter} invited you to play: {game_name}[/bold]", id="invite-title")
            yield Static("[dim]Enter to join  ·  Esc to decline[/dim]", id="invite-hint")

    async def action_accept(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        await app.send_line(f"/accept {self.gid}")

    async def action_decline(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        await app.send_line(f"/decline {self.gid}")
        self.dismiss()


class AbandonConfirmScreen(ModalScreen):
    """Confirmation popup before abandoning a game in progress."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, gid: str) -> None:
        super().__init__()
        self.gid = gid

    def compose(self) -> ComposeResult:
        with Vertical(id="invite-box"):
            yield Static("[bold]Are you sure you want to leave?[/bold]", id="invite-title")
            yield Menu(
                [("no", "No, keep playing"), ("yes", "Yes, leave")],
                show_hint=False,
                id="menu",
            )

    def on_mount(self) -> None:
        self.query_one(Menu).focus()

    def action_cancel(self) -> None:
        self.dismiss()

    def on_menu_selected(self, event: Menu.Selected) -> None:
        gid = self.gid
        self.dismiss()
        if event.value == "yes":
            app: "ShellGamesApp" = self.app  # type: ignore[assignment]
            app.run_worker(app.send_line(f"/forfeit {gid}"))


class QuitConfirmScreen(ModalScreen):
    """Confirmation popup shown on Ctrl+C or Esc, from anywhere the
    current screen doesn't already claim that key for its own back
    navigation. Offers a "Go to menu" shortcut too, but only when
    triggered from an actual game screen — no point offering it from
    the menu itself."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, from_game: bool = False) -> None:
        super().__init__()
        self.from_game = from_game

    def action_cancel(self) -> None:
        self.dismiss()

    def compose(self) -> ComposeResult:
        options = [("no", "No, keep playing")]
        if self.from_game:
            options.append(("menu", "Go to menu"))
        options.append(("yes", "Yes, quit"))
        with Vertical(id="invite-box"):
            yield Static("[bold]Quit Shell Games?[/bold]", id="invite-title")
            yield Menu(options, show_hint=False, id="menu")

    def on_mount(self) -> None:
        self.query_one(Menu).focus()

    def on_menu_selected(self, event: Menu.Selected) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if event.value == "yes":
            app.action_quit_clean()
        elif event.value == "menu":
            self.dismiss()
            app.leave_game_to_menu()  # type: ignore[attr-defined]
        else:
            self.dismiss()


class FinishableScreen(Screen):
    """Common to the 3 game screens: while playing, only "Back [q]" is
    shown (leave with confirmation); once finished that's hidden and a
    Menu with "Back" and "Play again" appears. In a best-of-N match, the
    host auto-starts the next round. If nobody returns to the menu within
    a minute, it goes back automatically."""

    AUTO_RETURN_SECONDS = 60
    ROUND_DELAY_SECONDS = 2.5

    def _init_match(self, kind: str, symbol: str, config: str) -> None:
        base, rounds, host_wins, guest_wins, round_num = parse_match_config(config)
        self.kind = kind
        self.base_config = base
        self.rounds_target = rounds
        self.host_wins = host_wins
        self.guest_wins = guest_wins
        self.round_num = round_num
        self.is_host = is_host_symbol(kind, symbol)
        self.match_over = rounds <= 1

    def _match_suffix(self) -> str:
        if self.rounds_target <= 1:
            return ""
        mine, theirs = (self.host_wins, self.guest_wins) if self.is_host else (self.guest_wins, self.host_wins)
        if self.match_over:
            extra = ""
        elif self.is_host:
            extra = f" — starting round {self.round_num + 1}/{self.rounds_target}..."
        else:
            extra = f" — waiting for {self.opponent} to start round {self.round_num + 1}/{self.rounds_target}..."
        return f" (round {self.round_num}/{self.rounds_target}, score {mine}-{theirs}){extra}"

    def _register_round_result(self, won: bool, is_draw: bool) -> None:
        if self.rounds_target <= 1:
            return
        if not is_draw:
            if won == self.is_host:
                self.host_wins += 1
            else:
                self.guest_wins += 1
        majority = self.rounds_target // 2 + 1
        self.match_over = (
            self.host_wins >= majority
            or self.guest_wins >= majority
            or self.round_num >= self.rounds_target
        )

    def _finish_menu_options(self) -> list[tuple[str, str]]:
        """"Play again" only makes sense once the whole match is over —
        mid-match the host is already auto-starting the next round."""
        options = [("back", "Back")]
        if self.match_over:
            options.append(("rematch", "Play again"))
        return options

    def _finish_common(self) -> None:
        self.finished = True
        self.query_one("#quit-hint", Static).display = False
        menu = self.query_one(Menu)
        menu.display = True
        menu.focus()
        self.set_timer(self.AUTO_RETURN_SECONDS, self._auto_return)
        self._continue_timer = None
        if not self.match_over and self.is_host:
            self._continue_timer = self.set_timer(self.ROUND_DELAY_SECONDS, self._continue_match)

    def _continue_match(self) -> None:
        self._continue_timer = None
        if self.app.screen is not self:
            return
        next_config = f"{self.base_config}:{self.rounds_target}:{self.host_wins}:{self.guest_wins}:{self.round_num + 1}"
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        app.run_worker(app.send_line(f"/play {self.opponent} {self.kind} {next_config}"))

    def _auto_return(self) -> None:
        if self.app.screen is self:
            self.app.pop_to_main()  # type: ignore[attr-defined]

    def on_menu_selected(self, event: Menu.Selected) -> None:
        if event.value == "back":
            if getattr(self, "_continue_timer", None) is not None:
                self._continue_timer.stop()
                self._continue_timer = None
            self.app.pop_to_main()  # type: ignore[attr-defined]
        elif event.value == "rematch":
            self._send_rematch()

    def _send_rematch(self) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        self.query_one(Menu).disabled = True
        self.query_one("#game-status", Static).update(f"[dim]Sending a rematch request to {self.opponent}...[/dim]")
        app.run_worker(app.send_line(f"/play {self.opponent} {self.kind} {self.base_config}"))

    def apply_connection_lost(self) -> None:
        """Called when the connection drops mid-game without the server
        ever sending !opponent_left (e.g. a network blip) — otherwise the
        player is just left staring at a frozen screen until the 60s
        auto-return timer (which only starts once finished anyway)."""
        self.match_over = True
        self._finish()
        self.query_one("#game-status", Static).update("[red]Connection to the server was lost[/red]")

    def _start_error_watch(self) -> None:
        self._error_task: asyncio.Task | None = asyncio.create_task(self._watch_errors())

    def _stop_error_watch(self) -> None:
        if getattr(self, "_error_task", None):
            self._error_task.cancel()

    def on_unmount(self) -> None:
        # NOT called from _finish(): a rematch can still be sent after the
        # game is "finished" (the Back/Play again menu), and its !error
        # needs a live watcher. Only stop once the screen actually leaves
        # the stack for good.
        self._stop_error_watch()

    async def _watch_errors(self) -> None:
        """Shows any server !error while this screen is active. Once the
        game/round is over, an error here means a follow-up action (like
        a rematch request) failed — re-enable the menu so the player
        isn't stuck looking at a disabled "Back"/"Play again"."""
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        while True:
            error = await app.error_queue.get()
            self.query_one("#game-status", Static).update(f"[red]{error}[/red]")
            if self.finished:
                self.query_one(Menu).disabled = False


class GameScreen(FinishableScreen):
    BINDINGS = [Binding("q", "confirm_abandon", "Leave")]

    def __init__(self, gid: str, opponent: str, symbol: str, config: str = "-") -> None:
        super().__init__()
        self.gid = gid
        self.opponent = opponent
        self.symbol = symbol
        self.turn = "X"
        self.finished = False
        self._init_match("ttt", symbol, config)
        self._error_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static("TIC-TAC-TOE", id="game-title")
            yield Static(f"vs {self.opponent}", id="game-opponent")
            yield Board(id="board")
            yield Static("", id="game-status")
            yield Static("[dim]Back \\[q][/dim]", id="quit-hint")
            yield Menu(self._finish_menu_options(), show_hint=False, id="menu")

    def on_mount(self) -> None:
        self.query_one(Menu).display = False
        self.apply_board("." * 9, "X")
        self.query_one(Board).focus()
        self._start_error_watch()

    def action_confirm_abandon(self) -> None:
        if not self.finished:
            self.app.push_screen(AbandonConfirmScreen(self.gid))

    def _update_status(self) -> None:
        status = self.query_one("#game-status", Static)
        if self.finished:
            return
        if self.turn == self.symbol:
            status.update("[green]Your turn[/green]")
        else:
            status.update(f"[dim]{self.opponent}'s turn[/dim]")

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
        is_draw = result == "draw"
        line = self._winning_line(board)
        self._register_round_result(won, is_draw)
        self._finish()
        board.set_result(line, own_win=won)
        suffix = self._match_suffix()
        if is_draw:
            text = "[yellow bold]Draw[/yellow bold]"
        elif reason == "timeout":
            text = f"[green bold]You won[/green bold] — {self.opponent} went inactive" if won else "[red bold]You lost due to inactivity[/red bold]"
        elif reason == "forfeit":
            text = f"[green bold]You won[/green bold] — {self.opponent} forfeited" if won else "[yellow bold]You forfeited the game[/yellow bold]"
        elif won:
            text = "[green bold]You won![/green bold]"
        else:
            text = f"[red bold]You lost[/red bold] — {self.opponent} won"
        self.query_one("#game-status", Static).update(text + suffix)

    def apply_opponent_left(self) -> None:
        self.match_over = True
        self._finish()
        self.query_one(Board).set_result(None, False)
        status = self.query_one("#game-status", Static)
        status.update(f"[red]{self.opponent} disconnected[/red]")

    def apply_connection_lost(self) -> None:
        self.match_over = True
        self._finish()
        self.query_one(Board).set_result(None, False)
        self.query_one("#game-status", Static).update("[red]Connection to the server was lost[/red]")

    def on_board_cell_selected(self, event: Board.CellSelected) -> None:
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        self.run_worker(app.send_line(f"/move {self.gid} {event.cell}"))


# ---------------------------------------------------------------------------
# Screens: hangman
# ---------------------------------------------------------------------------


class HangmanScreen(FinishableScreen):
    BINDINGS = [Binding("q", "confirm_abandon", "Leave")]

    def __init__(self, gid: str, opponent: str, role: str, config: str = "classic") -> None:
        super().__init__()
        self.gid = gid
        self.opponent = opponent
        self.role = role  # "setter"/"guesser" (classic) or "P1"/"P2" (race/turns)
        self._init_match("hangman", role, config)
        self.mode = self.base_config if self.base_config in HANGMAN_MODES else "classic"
        self.finished = False
        self.word_set = self.mode != "classic"
        self._error_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static("HANGMAN", id="game-title")
            yield Static(f"vs {self.opponent}", id="game-opponent")
            yield Static("", id="hm-word")
            yield Static("", id="hm-misses")
            yield Static("", id="game-status")
            yield Input(id="hm-input")
            yield Static("[dim]Back \\[q][/dim]", id="quit-hint")
            yield Menu(self._finish_menu_options(), show_hint=False, id="menu")

    def on_mount(self) -> None:
        self.query_one(Menu).display = False
        input_widget = self.query_one("#hm-input", Input)
        if self.mode == "race":
            input_widget.placeholder = "a letter"
            self.query_one("#hm-word", Static).update("[dim]Guess the word at your own pace[/dim]")
            self.query_one("#game-status", Static).update("[green]Guess a letter[/green]")
            input_widget.focus()
        elif self.mode == "turns":
            input_widget.placeholder = "a letter or the whole word"
            self.query_one("#hm-word", Static).update("[dim]One shared word — take turns guessing[/dim]")
            input_widget.focus()
        elif self.role == "setter":
            input_widget.placeholder = "type the secret word"
            self.query_one("#hm-word", Static).update("Choose a word for your opponent to guess")
            input_widget.focus()
        else:
            input_widget.placeholder = "a letter"
            input_widget.disabled = True
            self.query_one("#hm-word", Static).update("[dim]Waiting for your opponent to choose a word...[/dim]")
        self._start_error_watch()

    def action_confirm_abandon(self) -> None:
        if not self.finished:
            self.app.push_screen(AbandonConfirmScreen(self.gid))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip().lower()
        input_widget = self.query_one("#hm-input", Input)
        app: "ShellGamesApp" = self.app  # type: ignore[assignment]
        if self.mode == "classic" and self.role == "setter" and not self.word_set:
            if not value.isalpha() or not (3 <= len(value) <= 20):
                self.query_one("#game-status", Static).update("[red]Letters only, between 3 and 20[/red]")
                return
            await app.send_line(f"/hangword {self.gid} {value}")
            input_widget.value = ""
        elif self.mode == "turns" and not self.finished and not input_widget.disabled:
            if not value.isalpha():
                self.query_one("#game-status", Static).update("[red]Type a letter or the whole word[/red]")
                return
            await app.send_line(f"/guess {self.gid} {value}")
            input_widget.value = ""
        elif self.mode in ("classic", "race") and self.role in ("guesser", "P1", "P2") and self.word_set and not self.finished and not input_widget.disabled:
            letter = value[:1]
            if not letter.isalpha():
                self.query_one("#game-status", Static).update("[red]Type a letter[/red]")
                return
            await app.send_line(f"/guess {self.gid} {letter}")
            input_widget.value = ""

    def apply_state(self, revealed: str, misses: int, turn_symbol: str = "-") -> None:
        self.word_set = True
        self.query_one("#hm-word", Static).update(f"[bold]{' '.join(revealed.upper())}[/bold]")
        self.query_one("#hm-misses", Static).update(f"Misses: {misses}/{HANGMAN_MAX_MISSES}")
        input_widget = self.query_one("#hm-input", Input)
        status = self.query_one("#game-status", Static)
        if self.finished:
            return
        if self.mode == "race":
            if misses >= HANGMAN_MAX_MISSES:
                input_widget.disabled = True
                status.update(f"[red]You're out of guesses — waiting for {self.opponent}[/red]")
            else:
                input_widget.disabled = False
                status.update("[green]Guess a letter[/green]")
                input_widget.focus()
        elif self.mode == "turns":
            my_turn = turn_symbol == self.role
            input_widget.disabled = not my_turn
            if my_turn:
                status.update("[green]Your turn — guess a letter or the whole word[/green]")
                input_widget.focus()
            else:
                status.update(f"[dim]{self.opponent}'s turn[/dim]")
        elif self.role == "setter":
            input_widget.disabled = True
            status.update(f"[dim]Waiting for {self.opponent} to guess...[/dim]")
        else:
            input_widget.disabled = False
            status.update("[green]Guess a letter[/green]")
            input_widget.focus()

    def _finish(self) -> None:
        self.query_one("#hm-input", Input).disabled = True
        self._finish_common()

    def apply_over(self, result: str, reason: str = "normal") -> None:
        won = result == self.role
        is_draw = result == "draw"
        self._register_round_result(won, is_draw)
        self._finish()
        suffix = self._match_suffix()
        if is_draw:
            text = "[yellow bold]Draw[/yellow bold]"
        elif reason == "timeout":
            text = f"[green bold]You won[/green bold] — {self.opponent} went inactive" if won else "[red bold]You lost due to inactivity[/red bold]"
        elif reason == "forfeit":
            text = f"[green bold]You won[/green bold] — {self.opponent} forfeited" if won else "[yellow bold]You forfeited the game[/yellow bold]"
        elif won:
            text = "[green bold]You won![/green bold]"
        else:
            text = "[red bold]You lost[/red bold]"
        self.query_one("#game-status", Static).update(text + suffix)

    def apply_opponent_left(self) -> None:
        self.match_over = True
        self._finish()
        self.query_one("#game-status", Static).update(f"[red]{self.opponent} disconnected[/red]")


# ---------------------------------------------------------------------------
# Screens: battleship
# ---------------------------------------------------------------------------


class BSBoard(Static, can_focus=True):
    """8x8 battleship board, navigable with arrow keys."""

    CURSOR_STYLE = "black on #90ee90"

    BINDINGS = [
        Binding("left", "cursor_left", "Left"),
        Binding("right", "cursor_right", "Right"),
        Binding("up", "cursor_up", "Up"),
        Binding("down", "cursor_down", "Down"),
        Binding("enter", "select", "Confirm", priority=True),
        Binding("r", "rotate", "Rotate"),
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
        self.show_own = True  # True: own board (with ships); False: tracking the opponent
        self.ghost_len = 0  # length of the ship being previewed (placement phase)
        self.orientation = "h"
        self.compact = compact  # mini board (my fleet): no spacing between cells

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
        """The ship being placed must never be able to point off the
        board: this limits where the cursor can sit based on its current
        length and orientation."""
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
    """Controls reminder, shown once when entering the game."""

    BINDINGS = [
        Binding("enter", "dismiss_help", "Close", priority=True),
        Binding("escape", "dismiss_help", "Close", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="invite-box"):
            yield Static("[bold]Controls[/bold]", id="invite-title")
            yield Static("[dim]Arrows: move[/dim]", id="ch-1")
            yield Static("[dim]R: rotate the ship[/dim]", id="ch-2")
            yield Static("[dim]Enter: place / shoot[/dim]", id="ch-3")
            yield Static("[dim]Q: leave[/dim]", id="ch-4")
            yield Static("[dim]Enter to continue[/dim]", id="invite-hint2")

    def action_dismiss_help(self) -> None:
        self.dismiss()


class BattleshipScreen(FinishableScreen):
    BINDINGS = [Binding("q", "confirm_abandon", "Leave")]

    def __init__(self, gid: str, opponent: str, symbol: str, config: str = "classic") -> None:
        super().__init__()
        self.gid = gid
        self.opponent = opponent
        self.symbol = symbol
        self._init_match("battleship", symbol, config)
        preset = self.base_config if self.base_config in BATTLESHIP_PRESETS else "classic"
        self.ship_sizes = BATTLESHIP_PRESETS[preset]
        self.finished = False
        self.phase = "placing"
        self.next_ship = 0
        self.remaining_sizes = list(self.ship_sizes)
        self._error_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="main-box"):
            yield Static("BATTLESHIP", id="game-title")
            yield Static(f"vs {self.opponent}", id="game-opponent")
            yield Static("", id="game-status")
            with Horizontal(id="battle-row"):
                with Vertical(id="fleet-col"):
                    yield Static("[dim]Your fleet:[/dim]", id="fleet-label")
                    yield BSBoard(compact=True, id="own-board")
                with Vertical(id="tracking-col"):
                    yield Static("", id="remaining-label")
                    with Center():
                        yield BSBoard(id="board")
            yield Static("[dim]Back \\[q][/dim]", id="quit-hint")
            yield Menu(self._finish_menu_options(), show_hint=False, id="menu")

    def on_mount(self) -> None:
        self.query_one(Menu).display = False
        self.query_one("#fleet-col").display = False
        board = self.query_one("#board", BSBoard)
        board.interactive = True
        board.set_ghost_len(self.ship_sizes[0])
        board.focus()
        self._update_status()
        self._update_remaining_label()
        self._start_error_watch()
        self.app.push_screen(ControlsHelpScreen(), callback=lambda _: board.focus())

    def _update_status(self) -> None:
        if self.finished:
            return
        status = self.query_one("#game-status", Static)
        if self.phase == "placing":
            if self.next_ship < len(self.ship_sizes):
                size = self.ship_sizes[self.next_ship]
                status.update(f"Place your ship of size {size} ({self.next_ship + 1}/{len(self.ship_sizes)}) — [dim]r to rotate[/dim]")
            else:
                status.update("[dim]Waiting for your opponent to finish placing...[/dim]")

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
        """Counts how many of my own ships are already placed, based on
        the board the server sends (the source of truth, not a local
        counter that could get out of sync with the network)."""
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
            label.update(f"[dim]Enemy ships left to sink: {sizes}[/dim]")
        else:
            label.update("[green]You found the whole enemy fleet[/green]")

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
            self.query_one("#fleet-col").display = True
            # revealing a previously-hidden widget mid-session can leave a
            # stale incremental repaint in some terminals (the box grows
            # by 10 rows but the diff-based redraw doesn't always clear
            # the old blank region) — force a full relayout+repaint here.
            self.refresh(layout=True)
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
            status.update("[green]Your turn: choose where to shoot[/green]" if my_turn else f"[dim]{self.opponent}'s turn[/dim]")

    def _finish(self) -> None:
        self.query_one("#board", BSBoard).interactive = False
        self._finish_common()

    def apply_over(self, result: str, reason: str = "normal") -> None:
        won = result == self.symbol
        self._register_round_result(won, False)  # battleship has no draws
        self._finish()
        suffix = self._match_suffix()
        if reason == "timeout":
            text = f"[green bold]You won[/green bold] — {self.opponent} went inactive" if won else "[red bold]You lost due to inactivity[/red bold]"
        elif reason == "forfeit":
            text = f"[green bold]You won[/green bold] — {self.opponent} forfeited" if won else "[yellow bold]You forfeited the game[/yellow bold]"
        elif won:
            text = "[green bold]You sank the whole enemy fleet![/green bold]"
        else:
            text = "[red bold]Your fleet was sunk[/red bold]"
        self.query_one("#game-status", Static).update(text + suffix)

    def apply_opponent_left(self) -> None:
        self.match_over = True
        self._finish()
        self.query_one("#game-status", Static).update(f"[red]{self.opponent} disconnected[/red]")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class ShellGamesApp(App):
    BINDINGS = [
        Binding("ctrl+c", "confirm_quit", "Quit", show=False),
        Binding("escape", "confirm_quit", "Quit", show=False),
    ]

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
    #battle-row { width: 100%; height: auto; }
    #fleet-col { width: auto; height: auto; margin-right: 2; }
    #own-board { width: 8; height: 8; }
    #fleet-label { width: 8; height: 1; text-align: left; }
    #tracking-col { width: 1fr; height: auto; }
    #tracking-col BSBoard { width: auto; }
    #remaining-label { width: 100%; height: 1; text-align: left; }
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
        """Returns to the main menu, no matter how many screens are
        stacked on top (invite/code/game)."""
        while len(self.screen_stack) > 1 and not isinstance(self.screen, MainScreen):
            self.pop_screen()
        if isinstance(self.screen, MainScreen):
            # refocusing resets the menu's "cooldown": Enters that came
            # from mashing at the end of the previous game must not leak
            # in here and trigger an accidental navigation.
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
        """Waits for the connection to be ready. Returns False on timeout."""
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
                    await asyncio.wait_for(ws.recv(), timeout=10)  # "Name: "
                    # use self.username (not the original parameter) so a
                    # reconnect honors a /rename done before the drop
                    username = self.username or username
                    self.username = username
                    await ws.send(username)
                    await asyncio.wait_for(ws.recv(), timeout=10)  # "Connected as ..."
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
                game_screens = (GameScreen, HangmanScreen, BattleshipScreen)
                if isinstance(self.screen, game_screens) and not self.screen.finished:
                    self.screen.apply_connection_lost()
            await asyncio.sleep(2)  # retry the connection

    def _dispatch(self, msg: str) -> None:
        game_screens = (GameScreen, HangmanScreen, BattleshipScreen)
        if msg.startswith("* online"):
            self._who_queue.put_nowait(msg)
        elif msg.startswith("!invite "):
            _, gid, inviter, kind, config = msg.split()
            if isinstance(self.screen, game_screens) and not self.screen.finished:
                # server already refuses to invite a busy player, but
                # just in case one still slips through (a race right as
                # a game starts), auto-decline instead of popping a
                # modal over an active game — it would silently eat
                # arrow-key input meant for the board underneath.
                self.run_worker(self.send_line(f"/decline {gid}"))
                return
            if isinstance(self.screen, game_screens) and self.screen.finished:
                # don't leave the old finished game underneath in the
                # stack: if this invite is accepted and then "Back" is
                # pressed in the new game, it must go to the menu, not
                # the old screen.
                self.pop_to_main()
            self.push_screen(InvitePopup(gid, inviter, kind))
        elif msg.startswith("!start "):
            _, gid, opponent, symbol, kind, config = msg.split()
            if kind == "hangman":
                self.switch_screen(HangmanScreen(gid, opponent, symbol, config))
            elif kind == "battleship":
                self.switch_screen(BattleshipScreen(gid, opponent, symbol, config))
            else:
                self.switch_screen(GameScreen(gid, opponent, symbol, config))
        elif msg.startswith("!board "):
            _, gid, board, turn = msg.split()
            if isinstance(self.screen, GameScreen) and self.screen.gid == gid:
                self.screen.apply_board(board, turn)
        elif msg.startswith("!hm_state "):
            _, gid, revealed, misses, turn_symbol = msg.split()
            if isinstance(self.screen, HangmanScreen) and self.screen.gid == gid:
                self.screen.apply_state(revealed, int(misses), turn_symbol)
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
            self.error_queue.put_nowait(f"{who} declined the invitation")
        elif msg.startswith("!invited "):
            pass  # silent confirmation

    async def request_who(self) -> str:
        if not self.connected or self.ws is None:
            return "Disconnected from the server."
        await self.ws.send("/who")
        try:
            return await asyncio.wait_for(self._who_queue.get(), timeout=5)
        except asyncio.TimeoutError:
            return "No response from the server."

    def action_confirm_quit(self) -> None:
        if isinstance(self.screen, QuitConfirmScreen):
            return
        game_screens = (GameScreen, HangmanScreen, BattleshipScreen)
        self.push_screen(QuitConfirmScreen(from_game=isinstance(self.screen, game_screens)))

    def leave_game_to_menu(self) -> None:
        game_screens = (GameScreen, HangmanScreen, BattleshipScreen)
        screen = self.screen
        if isinstance(screen, game_screens) and not screen.finished:
            self.run_worker(self.send_line(f"/forfeit {screen.gid}"))
        self.pop_to_main()

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
