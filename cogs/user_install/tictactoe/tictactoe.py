import discord
from discord.ext import commands
from discord import app_commands
from discord.ext.commands import Cog
import logging
import json
import os
from typing import Optional, List, Tuple, Dict

logging.basicConfig(level=logging.ERROR)

# ──────────────────────────────────────────────────────────────────────────────
# Load Emoji Configuration from config.json
# Edit config.json in this folder to customize emojis.
# ──────────────────────────────────────────────────────────────────────────────

def load_emoji_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        emojis = config.get("emojis", {})
        text_fallbacks = config.get("text_fallbacks", {})

        def get_emoji(key, fallback_text):
            emoji_data = emojis.get(key, {})
            emoji_id = emoji_data.get("id", 0)
            emoji_name = emoji_data.get("name", "")

            # If ID is 0 or None, return None to use label text instead
            if not emoji_id or emoji_id == 0:
                return None

            return discord.PartialEmoji(name=emoji_name, id=emoji_id)

        return {
            "EMOJI_X":     get_emoji("x",     None),
            "EMOJI_O":     get_emoji("o",     None),
            "EMOJI_BLANK": get_emoji("blank", None),
            "EMOJI_WIN":   get_emoji("win",   None),
            "TEXT_X":      text_fallbacks.get("x",  "✕"),
            "TEXT_O":      text_fallbacks.get("o",  "○"),
        }
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.error(f"Failed to load emoji config: {e}. Using defaults.")
        return {
            "EMOJI_X":     None,
            "EMOJI_O":     None,
            "EMOJI_BLANK": None,
            "EMOJI_WIN":   None,
            "TEXT_X":      "✕",
            "TEXT_O":      "○",
        }

_emoji_config = load_emoji_config()
EMOJI_X     = _emoji_config["EMOJI_X"]
EMOJI_O     = _emoji_config["EMOJI_O"]
EMOJI_BLANK = _emoji_config["EMOJI_BLANK"]
EMOJI_WIN   = _emoji_config["EMOJI_WIN"]
TEXT_X      = _emoji_config["TEXT_X"]
TEXT_O      = _emoji_config["TEXT_O"]

# ──────────────────────────────────────────────────────────────────────────────
# Winning Combinations  (x, y) — board is indexed board[y][x]
# ──────────────────────────────────────────────────────────────────────────────

WINNING_COMBOS: List[List[Tuple[int, int]]] = [
    [(0, 0), (1, 0), (2, 0)],
    [(0, 1), (1, 1), (2, 1)],
    [(0, 2), (1, 2), (2, 2)],
    [(0, 0), (0, 1), (0, 2)],
    [(1, 0), (1, 1), (1, 2)],
    [(2, 0), (2, 1), (2, 2)],
    [(0, 0), (1, 1), (2, 2)],
    [(2, 0), (1, 1), (0, 2)],
]

# ──────────────────────────────────────────────────────────────────────────────
# Embed Colours
# ──────────────────────────────────────────────────────────────────────────────

COLOR_ACTIVE  = 0x5865F2
COLOR_WIN     = 0x57F287
COLOR_TIE     = 0xFEE75C
COLOR_FORFEIT = 0xED4245
COLOR_INVITE  = 0xEB459E

# ──────────────────────────────────────────────────────────────────────────────
# Module-level game state
# ──────────────────────────────────────────────────────────────────────────────

active_challenges:    Dict[frozenset, bool]            = {}
active_game_sessions: Dict[frozenset, "TicTacToeView"] = {}


# ──────────────────────────────────────────────────────────────────────────────
# Embed Builders
# ──────────────────────────────────────────────────────────────────────────────

def _score_line(scores: Dict, x_id: int, o_id: int) -> str:
    return (
        f"{TEXT_X} <@{x_id}>  `{scores.get(x_id, 0)}`"
        f"   ·   "
        f"{TEXT_O} <@{o_id}>  `{scores.get(o_id, 0)}`"
        f"   ·   "
        f"Draws  `{scores.get('ties', 0)}`"
    )


def build_turn_embed(view: "TicTacToeView") -> discord.Embed:
    is_x   = view.current_player_id == view.player_x_id
    symbol = TEXT_X if is_x else TEXT_O
    embed  = discord.Embed(
        title       = "Tic-Tac-Toe",
        description = f"<@{view.current_player_id}>'s turn  **({symbol})**",
        color       = COLOR_ACTIVE,
    )
    embed.add_field(
        name   = "Score",
        value  = _score_line(view.scores, view.player_x_id, view.player_o_id),
        inline = False,
    )
    embed.set_footer(text="Game expires after 5 minutes of inactivity.")
    return embed


def build_end_embed(view: "TicTacToeView", winner: int) -> discord.Embed:
    if winner == TicTacToeView.X:
        desc  = f"{TEXT_X} <@{view.player_x_id}> wins"
        color = COLOR_WIN
    elif winner == TicTacToeView.O:
        desc  = f"{TEXT_O} <@{view.player_o_id}> wins"
        color = COLOR_WIN
    else:
        desc  = "It's a draw"
        color = COLOR_TIE

    embed = discord.Embed(title="Game Over", description=desc, color=color)
    embed.add_field(
        name   = "Score",
        value  = _score_line(view.scores, view.player_x_id, view.player_o_id),
        inline = False,
    )
    return embed


def build_forfeit_embed(view: "TicTacToeView", forfeiter_id: int, winner_id: int) -> discord.Embed:
    embed = discord.Embed(
        title       = "Game Over",
        description = f"<@{forfeiter_id}> forfeited — <@{winner_id}> wins",
        color       = COLOR_FORFEIT,
    )
    embed.add_field(
        name   = "Score",
        value  = _score_line(view.scores, view.player_x_id, view.player_o_id),
        inline = False,
    )
    return embed


def build_timeout_embed() -> discord.Embed:
    return discord.Embed(
        title       = "Game Over",
        description = "This game expired due to inactivity.",
        color       = COLOR_FORFEIT,
    )


def build_challenge_embed(challenger_id: int, opponent_id: int) -> discord.Embed:
    embed = discord.Embed(
        title       = "Game Request",
        description = (
            f"<@{challenger_id}> has invited <@{opponent_id}> "
            f"to a game of Tic-Tac-Toe."
        ),
        color = COLOR_INVITE,
    )
    embed.set_footer(text="This invitation expires in 60 seconds.")
    return embed


def build_declined_embed(decliner_id: int) -> discord.Embed:
    return discord.Embed(
        description = f"<@{decliner_id}> declined the invitation.",
        color       = COLOR_FORFEIT,
    )


def build_expired_challenge_embed(opponent_id: int) -> discord.Embed:
    return discord.Embed(
        description = f"<@{opponent_id}> did not respond in time.",
        color       = COLOR_FORFEIT,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Board Button
# ──────────────────────────────────────────────────────────────────────────────

class TicTacToeButton(discord.ui.Button):

    def __init__(self, x: int, y: int) -> None:
        # FIX 1: label logic was inverted.
        # When EMOJI_BLANK is set   → show the emoji, no text label (label=None).
        # When EMOJI_BLANK is None  → no emoji available, must show a text label.
        # The original code did the opposite: label="·" only when EMOJI_BLANK
        # existed, leaving label=None (and emoji=None) when there was no custom
        # emoji — an invalid Discord button state that breaks rendering.
        super().__init__(
            style = discord.ButtonStyle.secondary,
            emoji = EMOJI_BLANK,
            label = None if EMOJI_BLANK else "·",
            row   = y,
        )
        self.x = x
        self.y = y

    async def callback(self, interaction: discord.Interaction) -> None:
        view: TicTacToeView = self.view  # type: ignore[assignment]

        if view is None:
            await interaction.response.send_message(
                "This game is no longer active.", ephemeral=True
            )
            return

        if interaction.user.id not in (view.player_x_id, view.player_o_id):
            await interaction.response.send_message(
                "You are not a player in this game.", ephemeral=True
            )
            return

        if interaction.user.id != view.current_player_id:
            await interaction.response.send_message(
                "It is not your turn.", ephemeral=True
            )
            return

        if view.board[self.y][self.x] != 0:
            await interaction.response.send_message(
                "That cell is already occupied.", ephemeral=True
            )
            return

        # ── Place piece ──────────────────────────────────────────────────────
        if view.current_player_id == view.player_x_id:
            self.style              = discord.ButtonStyle.danger
            self.emoji              = EMOJI_X
            self.label              = TEXT_X if not EMOJI_X else None
            view.board[self.y][self.x] = view.X
            view.current_player_id     = view.player_o_id
        else:
            self.style              = discord.ButtonStyle.primary
            self.emoji              = EMOJI_O
            self.label              = TEXT_O if not EMOJI_O else None
            view.board[self.y][self.x] = view.O
            view.current_player_id     = view.player_x_id

        self.disabled = True

        # ── Check result ─────────────────────────────────────────────────────
        winner, winning_combo = view.check_winner()

        if winner is not None:
            view.handle_game_end(winner, winning_combo)
            await interaction.response.edit_message(
                embed = build_end_embed(view, winner),
                view  = view,
            )
        else:
            await interaction.response.edit_message(
                embed = build_turn_embed(view),
                view  = view,
            )


# ──────────────────────────────────────────────────────────────────────────────
# Forfeit Button  (row 3)
# ──────────────────────────────────────────────────────────────────────────────

class ForfeitButton(discord.ui.Button):

    def __init__(self) -> None:
        super().__init__(
            style = discord.ButtonStyle.secondary,
            label = "Forfeit",
            row   = 3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: TicTacToeView = self.view  # type: ignore[assignment]

        if view is None:
            await interaction.response.send_message(
                "This game is no longer active.", ephemeral=True
            )
            return

        if interaction.user.id not in (view.player_x_id, view.player_o_id):
            await interaction.response.send_message(
                "You are not a player in this game.", ephemeral=True
            )
            return

        forfeiter_id = interaction.user.id
        winner_id    = (
            view.player_o_id if forfeiter_id == view.player_x_id
            else view.player_x_id
        )
        view.scores[winner_id] = view.scores.get(winner_id, 0) + 1

        view._lock_board()
        view._cleanup_session()

        for child in list(view.children):
            if isinstance(child, ForfeitButton):
                view.remove_item(child)

        view.add_item(RematchButton())

        await interaction.response.edit_message(
            embed = build_forfeit_embed(view, forfeiter_id, winner_id),
            view  = view,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Rematch Button  (row 4, appended only after the game ends)
# ──────────────────────────────────────────────────────────────────────────────

class RematchButton(discord.ui.Button):

    def __init__(self) -> None:
        super().__init__(
            style = discord.ButtonStyle.primary,
            label = "Rematch",
            row   = 4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: TicTacToeView = self.view  # type: ignore[assignment]

        if view is None:
            await interaction.response.send_message(
                "This game is no longer active.", ephemeral=True
            )
            return

        if interaction.user.id not in (view.player_x_id, view.player_o_id):
            await interaction.response.send_message(
                "You are not a player in this game.", ephemeral=True
            )
            return

        key = frozenset([view.player_x_id, view.player_o_id])
        if key in active_game_sessions:
            await interaction.response.send_message(
                "A rematch is already in progress.", ephemeral=True
            )
            return

        # Disable all buttons immediately to prevent double-clicks
        self.disabled = True
        for child in view.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        # Swap X / O, carry over session scores
        new_view = TicTacToeView(
            player_x_id = view.player_o_id,
            player_o_id = view.player_x_id,
            scores      = view.scores,
        )
        active_game_sessions[key] = new_view

        await interaction.response.edit_message(
            embed = build_turn_embed(new_view),
            view  = new_view,
        )
        new_view.message = await interaction.original_response()


# ──────────────────────────────────────────────────────────────────────────────
# Game View
# ──────────────────────────────────────────────────────────────────────────────

class TicTacToeView(discord.ui.View):
    X   = -1
    O   =  1
    Tie =  2

    def __init__(
        self,
        player_x_id: int,
        player_o_id: int,
        scores: Optional[Dict] = None,
    ) -> None:
        super().__init__(timeout=300)

        self.player_x_id       = player_x_id
        self.player_o_id       = player_o_id
        self.current_player_id = player_x_id
        self.board: List[List[int]] = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
        self.message: Optional[discord.Message] = None

        # FIX 2 (part a): track whether the game has concluded so that
        # on_timeout knows not to overwrite the result embed.
        self.game_over: bool = False

        self.scores: Dict = scores or {
            player_x_id: 0,
            player_o_id: 0,
            "ties": 0,
        }

        self._buttons: List[TicTacToeButton] = []
        for y in range(3):
            for x in range(3):
                btn = TicTacToeButton(x, y)
                self.add_item(btn)
                self._buttons.append(btn)

        self.add_item(ForfeitButton())

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_button(self, x: int, y: int) -> Optional[TicTacToeButton]:
        for btn in self._buttons:
            if btn.x == x and btn.y == y:
                return btn
        return None

    def check_winner(
        self,
    ) -> Tuple[Optional[int], Optional[List[Tuple[int, int]]]]:
        for combo in WINNING_COMBOS:
            vals = [self.board[y][x] for x, y in combo]
            if vals == [self.X, self.X, self.X]:
                return self.X, combo
            if vals == [self.O, self.O, self.O]:
                return self.O, combo

        if all(self.board[y][x] != 0 for y in range(3) for x in range(3)):
            return self.Tie, None

        return None, None

    def handle_game_end(
        self,
        winner: int,
        winning_combo: Optional[List[Tuple[int, int]]],
    ) -> None:
        if winner == self.X:
            self.scores[self.player_x_id] = self.scores.get(self.player_x_id, 0) + 1
            if winning_combo:
                for x, y in winning_combo:
                    btn = self._get_button(x, y)
                    if btn:
                        btn.style = discord.ButtonStyle.success
                        if EMOJI_WIN:
                            btn.emoji = EMOJI_WIN
                            btn.label = None
        elif winner == self.O:
            self.scores[self.player_o_id] = self.scores.get(self.player_o_id, 0) + 1
            if winning_combo:
                for x, y in winning_combo:
                    btn = self._get_button(x, y)
                    if btn:
                        btn.style = discord.ButtonStyle.success
                        if EMOJI_WIN:
                            btn.emoji = EMOJI_WIN
                            btn.label = None
        else:
            self.scores["ties"] = self.scores.get("ties", 0) + 1

        self._lock_board()
        self._cleanup_session()

        for child in list(self.children):
            if isinstance(child, ForfeitButton):
                self.remove_item(child)

        self.add_item(RematchButton())

    def _lock_board(self) -> None:
        # FIX 2 (part b): do NOT call self.stop() here.
        #
        # discord.py's View.stop() sets an internal _stopped flag that causes
        # _dispatch_item() to silently ignore every future interaction on this
        # view — including the RematchButton that we are about to add.
        # Calling stop() therefore makes the rematch button permanently broken.
        #
        # Instead we set game_over=True and let the view's natural timeout
        # handle cleanup.  on_timeout() checks this flag before deciding
        # whether to overwrite the message.
        self.game_over = True
        for child in self.children:
            if isinstance(child, (TicTacToeButton, ForfeitButton)):
                child.disabled = True

    def _cleanup_session(self) -> None:
        active_game_sessions.pop(
            frozenset([self.player_x_id, self.player_o_id]), None
        )

    async def on_timeout(self) -> None:
        # FIX 3: Guard against overwriting a legitimately finished game.
        #
        # Without this check, if the 300-second timer fires after the game
        # already ended (win/draw/forfeit), the timeout embed would overwrite
        # the result embed and the disabled Rematch button would be re-shown
        # as enabled — confusing players and hiding the actual outcome.
        if self.game_over:
            # FIX 4: The game ended cleanly but the Rematch button was never
            # clicked before the view's timeout expired.  Disable it so it
            # doesn't appear clickable after discord.py removes the view from
            # its internal store.
            for child in self.children:
                child.disabled = True
            if self.message:
                try:
                    await self.message.edit(view=self)
                except (discord.NotFound, discord.HTTPException):
                    pass
            return

        # Normal timeout path: game was abandoned mid-play.
        self._lock_board()
        self._cleanup_session()
        if self.message:
            try:
                await self.message.edit(
                    embed = build_timeout_embed(),
                    view  = self,
                )
            except (discord.NotFound, discord.HTTPException):
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Challenge View
# ──────────────────────────────────────────────────────────────────────────────

class ChallengeView(discord.ui.View):

    def __init__(self, challenger_id: int, opponent_id: int) -> None:
        super().__init__(timeout=60)
        self.challenger_id = challenger_id
        self.opponent_id   = opponent_id
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message(
                "This invitation is not for you.", ephemeral=True
            )
            return

        key = frozenset([self.challenger_id, self.opponent_id])
        if key in active_game_sessions:
            await interaction.response.send_message(
                "A game between you two is already active.", ephemeral=True
            )
            return

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        active_challenges.pop(key, None)
        self.stop()

        game_view = TicTacToeView(self.challenger_id, self.opponent_id)
        active_game_sessions[key] = game_view

        await interaction.response.edit_message(
            embed = build_turn_embed(game_view),
            view  = game_view,
        )
        game_view.message = await interaction.original_response()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id not in (self.challenger_id, self.opponent_id):
            await interaction.response.send_message(
                "This invitation is not for you.", ephemeral=True
            )
            return

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        key = frozenset([self.challenger_id, self.opponent_id])
        active_challenges.pop(key, None)
        self.stop()

        await interaction.response.edit_message(
            embed = build_declined_embed(interaction.user.id),
            view  = None,
        )

    async def on_timeout(self) -> None:
        key = frozenset([self.challenger_id, self.opponent_id])
        active_challenges.pop(key, None)
        if self.message:
            try:
                await self.message.edit(
                    embed = build_expired_challenge_embed(self.opponent_id),
                    view  = None,
                )
            except (discord.NotFound, discord.HTTPException):
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class TicTacToeUserInstall(Cog):
    """
    Tic-Tac-Toe — User Install (DMs / group DMs only).

    /tictactoe <opponent>   Send a game invitation.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name        = "tictactoe",
        description = "Invite a friend to a game of Tic-Tac-Toe",
    )
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
    @app_commands.describe(opponent="The user you want to challenge")
    async def tictactoe(
        self,
        interaction: discord.Interaction,
        opponent: discord.User,
    ) -> None:
        if opponent.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot challenge yourself.", ephemeral=True
            )
            return

        if opponent.bot:
            await interaction.response.send_message(
                "You cannot challenge a bot.", ephemeral=True
            )
            return

        key = frozenset([interaction.user.id, opponent.id])

        if key in active_challenges:
            await interaction.response.send_message(
                "There is already a pending invitation between you two.",
                ephemeral=True,
            )
            return

        if key in active_game_sessions:
            await interaction.response.send_message(
                "You already have an active game with this player.",
                ephemeral=True,
            )
            return

        active_challenges[key] = True
        view = ChallengeView(interaction.user.id, opponent.id)

        await interaction.response.send_message(
            embed = build_challenge_embed(interaction.user.id, opponent.id),
            view  = view,
        )
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicTacToeUserInstall(bot))