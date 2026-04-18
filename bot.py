import asyncio
import json
import os
import random
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

import discord
from discord.ext import commands

try:
    from rapidfuzz import fuzz
except ImportError:
    from difflib import SequenceMatcher

    class FallbackFuzz:
        @staticmethod
        def ratio(left, right):
            return round(SequenceMatcher(None, left, right).ratio() * 100)

        @staticmethod
        def partial_ratio(left, right):
            if len(left) > len(right):
                left, right = right, left
            if not left:
                return 0
            best = 0
            for index in range(0, len(right) - len(left) + 1):
                score = SequenceMatcher(None, left, right[index : index + len(left)]).ratio()
                best = max(best, score)
            return round(best * 100)

        @staticmethod
        def token_set_ratio(left, right):
            left_tokens = set(left.split())
            right_tokens = set(right.split())
            if not left_tokens or not right_tokens:
                return 0
            common = left_tokens & right_tokens
            left_combo = " ".join(sorted(common | (left_tokens - right_tokens)))
            right_combo = " ".join(sorted(common | (right_tokens - left_tokens)))
            return FallbackFuzz.ratio(left_combo, right_combo)

    fuzz = FallbackFuzz()

# =========================
# BOT SETUP
# =========================

BASE_DIR = Path(__file__).resolve().parent
QUESTIONS_FILE = BASE_DIR / "questions.json"
DB_NAME = BASE_DIR / "ava.db"
AVA_PURPLE = discord.Color.from_rgb(126, 87, 194)
AVA_DEEP_PURPLE = discord.Color.from_rgb(74, 45, 121)
AVA_GOLD = discord.Color.from_rgb(214, 168, 72)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
active_games = {}


def ava_embed(title, description=None, color=AVA_PURPLE):
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_author(name="Ava")
    embed.set_footer(text="ASOIAF Trivia")
    return embed


def progress_bar(current, total, width=12):
    filled = round((current / total) * width)
    return "[" + ("#" * filled) + ("-" * (width - filled)) + f"] {current}/{total}"


def place_label(index):
    labels = {1: "1st", 2: "2nd", 3: "3rd"}
    return labels.get(index, f"{index}th")


def split_question_label(question):
    match = re.match(r"^\[(.*?)\]\s*(.*)$", question)
    if not match:
        return "TRIVIA", question
    return match.group(1), match.group(2)

# =========================
# LOAD QUESTIONS
# =========================

with QUESTIONS_FILE.open("r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)

# =========================
# DATABASE
# =========================


def db():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS leaderboard (
            user_id TEXT,
            guild_id TEXT,
            points INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            games INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, guild_id)
        )
        """
    )

    conn.commit()
    conn.close()


def update_stats(user_id, guild_id, points=0, win=False):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR IGNORE INTO leaderboard (user_id, guild_id, points, wins, games)
        VALUES (?, ?, 0, 0, 0)
        """,
        (str(user_id), str(guild_id)),
    )

    cur.execute(
        """
        UPDATE leaderboard
        SET points = points + ?,
            games = games + 1,
            wins = wins + ?
        WHERE user_id = ? AND guild_id = ?
        """,
        (points, 1 if win else 0, str(user_id), str(guild_id)),
    )

    conn.commit()
    conn.close()


def get_leaderboard(guild_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT user_id, points, wins
        FROM leaderboard
        WHERE guild_id = ?
        ORDER BY points DESC
        """,
        (str(guild_id),),
    )

    rows = cur.fetchall()
    conn.close()
    return rows


init_db()

# =========================
# ANSWER MATCHING
# =========================


def normalize_text(value):
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\b(the|a|an|ser|sir)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def split_alternatives(value):
    parts = [value]
    parts.extend(re.findall(r"\((.*?)\)", value))

    expanded = []
    for part in parts:
        part = re.sub(r"accept either.*", "", part, flags=re.IGNORECASE)
        part = re.sub(r"contextual answers accepted.*", "", part, flags=re.IGNORECASE)
        part = re.sub(r"correct answer:", "", part, flags=re.IGNORECASE)
        expanded.extend(re.split(r"\s+or\s+|/|;", part, flags=re.IGNORECASE))

    variants = set()
    for part in expanded:
        cleaned = re.sub(r"\(.*?\)", "", part).strip(" -,.")
        normalized = normalize_text(cleaned)
        if normalized:
            variants.add(normalized)

    no_brackets = normalize_text(re.sub(r"\(.*?\)", "", value))
    if no_brackets:
        variants.add(no_brackets)

    return variants


def answer_matches(guess, answer):
    normalized_guess = normalize_text(guess)
    if not normalized_guess:
        return False

    for variant in split_alternatives(answer):
        if normalized_guess == variant:
            return True

        # Short answers such as "Ice" or "Dog" should not be too fuzzy.
        if len(variant) <= 4:
            if fuzz.ratio(normalized_guess, variant) >= 92:
                return True
            continue

        if fuzz.ratio(normalized_guess, variant) >= 82:
            return True
        if fuzz.token_set_ratio(normalized_guess, variant) >= 86:
            return True
        if len(variant) >= 8 and fuzz.partial_ratio(normalized_guess, variant) >= 92:
            return True

    return False

# =========================
# GAME CLASS
# =========================


class AvaGame:
    def __init__(self, guild_id, channel):
        self.guild_id = guild_id
        self.channel = channel

        self.players = set()
        self.scores = {}
        self.streaks = {}
        self.lobby_message = None
        self.task = None

        self.accepting = False
        self.current_answer = None
        self.state = "lobby"
        self.topic = "asoiaf"

        self.rounds = 6
        self.questions_per_round = 5

        self.correct_lines = [
            "is correct!",
            "got it right!",
            "nailed it!",
            "that's right!",
        ]

    # -------------------------
    # PLAYER MANAGEMENT
    # -------------------------

    def join(self, user):
        self.players.add(user.id)
        self.scores.setdefault(user.id, 0)
        self.streaks.setdefault(user.id, 0)

    def leave(self, user):
        self.players.discard(user.id)
        self.scores.pop(user.id, None)
        self.streaks.pop(user.id, None)

    # -------------------------
    # LOBBY UI
    # -------------------------

    def build_lobby_embed(self):
        players = "\n".join(f"<@{player_id}>" for player_id in self.players) or "No players yet"

        embed = ava_embed(
            "Ava's Great Hall",
            description=(
                "**A new ASOIAF trivia table is forming.**\n\n"
                "Take a seat with **Join**, step away with **Leave**, and begin the game with **Start**."
            ),
            color=AVA_DEEP_PURPLE,
        )

        embed.add_field(name=f"Players ({len(self.players)})", value=players, inline=False)
        embed.add_field(name="Status", value=f"`{self.state.upper()}`", inline=True)
        embed.add_field(
            name="Game Format",
            value=(
                f"{self.rounds} rounds, {self.questions_per_round} questions per round\n"
                "Timers: 35s early, 30s middle, 25s final rounds"
            ),
            inline=True,
        )
        embed.add_field(
            name="Answer Rules",
            value="Minor typos count. Bracketed alternatives count. Fast answers can earn a bonus.",
            inline=False,
        )
        embed.set_footer(text="Buttons and slash commands both work.")

        return embed

    async def update_lobby(self):
        embed = self.build_lobby_embed()
        view = AvaLobbyView(self) if self.state == "lobby" else None

        if self.lobby_message:
            await self.lobby_message.edit(embed=embed, view=view)
        else:
            self.lobby_message = await self.channel.send(embed=embed, view=view)

    # -------------------------
    # GAME LOGIC
    # -------------------------

    def get_timer(self, round_num):
        if round_num <= self.rounds * 0.33:
            return 35
        if round_num <= self.rounds * 0.66:
            return 30
        return 25

    def is_correct(self, message):
        if not self.accepting:
            return False
        if message.guild is None or message.guild.id != self.guild_id:
            return False
        if message.channel.id != self.channel.id:
            return False
        if message.author.bot:
            return False
        if message.author.id not in self.players:
            return False

        return answer_matches(message.content, self.current_answer)

    def streak_bonus(self, streak):
        if streak >= 7:
            return 3
        if streak >= 5:
            return 2
        if streak >= 3:
            return 1
        return 0

    async def ava_say(self, message):
        embed = ava_embed("Ava Speaks", message, AVA_PURPLE)
        await self.channel.send(embed=embed)

    def build_question_embed(self, question, round_num, question_num, timer):
        label, clean_question = split_question_label(question)
        overall_question = ((round_num - 1) * self.questions_per_round) + question_num
        total_questions = self.rounds * self.questions_per_round

        embed = ava_embed(
            f"Round {round_num} - Question {question_num}",
            description=f">>> **{clean_question}**",
            color=AVA_DEEP_PURPLE,
        )
        embed.add_field(name="Source", value=f"`{label}`", inline=True)
        embed.add_field(name="Time Limit", value=f"`{timer}s`", inline=True)
        embed.add_field(name="Progress", value=progress_bar(overall_question, total_questions), inline=False)
        embed.add_field(
            name="How To Answer",
            value="Type your answer in chat. Small spelling mistakes are accepted.",
            inline=False,
        )
        embed.set_footer(text="First accepted answer wins the point.")
        return embed

    async def player_name(self, user_id):
        member = self.channel.guild.get_member(user_id)
        if member:
            return member.display_name
        user = await bot.fetch_user(user_id)
        return user.name

    async def build_score_lines(self, leaderboard):
        lines = []
        for index, (player_id, score) in enumerate(leaderboard, 1):
            name = await self.player_name(player_id)
            streak = self.streaks.get(player_id, 0)
            streak_text = f" | streak {streak}" if streak else ""
            lines.append(f"**{place_label(index)}** - {name}: **{score} pts**{streak_text}")
        return lines

    # -------------------------
    # GAME LOOP
    # -------------------------

    async def run_game(self):
        try:
            if not self.players:
                await self.ava_say("No players joined, so I cancelled the game.")
                active_games.pop(self.guild_id, None)
                return

            questions = list(QUESTIONS[self.topic])
            random.shuffle(questions)

            self.state = "active"
            await self.update_lobby()

            q_index = 0

            for round_num in range(1, self.rounds + 1):
                if not self.players:
                    await self.ava_say("Everyone left, so I ended the game.")
                    active_games.pop(self.guild_id, None)
                    return

                await self.ava_say(
                    f"**Round {round_num} begins in 5 seconds.**\n"
                    "Summon your lore, watch the timer, and answer in chat."
                )
                await asyncio.sleep(5)
                await self.ava_say(
                    f"**{self.questions_per_round} questions this round.**\n"
                    "Ava is listening for the first close-enough answer."
                )

                for q_num in range(1, self.questions_per_round + 1):
                    if not self.players:
                        await self.ava_say("Everyone left, so I ended the game.")
                        active_games.pop(self.guild_id, None)
                        return

                    q = questions[q_index % len(questions)]
                    q_index += 1

                    self.current_answer = q["answer"]
                    self.accepting = True

                    timer = self.get_timer(round_num)

                    await self.channel.send(
                        embed=self.build_question_embed(q["question"], round_num, q_num, timer)
                    )

                    start_time = asyncio.get_running_loop().time()

                    try:
                        msg = await bot.wait_for(
                            "message",
                            timeout=timer,
                            check=self.is_correct,
                        )
                        winner = msg.author
                        response_time = asyncio.get_running_loop().time() - start_time
                    except asyncio.TimeoutError:
                        self.accepting = False
                        await self.ava_say(
                            f"Nobody got it. The correct answer was **{self.current_answer}**"
                        )
                        continue

                    self.accepting = False
                    uid = winner.id

                    self.streaks[uid] = self.streaks.get(uid, 0) + 1
                    bonus = self.streak_bonus(self.streaks[uid])

                    for player_id in self.players:
                        if player_id != uid:
                            self.streaks[player_id] = 0

                    speed_bonus = 0
                    if response_time <= 3:
                        speed_bonus = 1
                        await self.ava_say(f"**Speed bonus** for {winner.display_name}.")

                    if self.streaks[uid] >= 3:
                        await self.ava_say(
                            f"**Streak watch:** {winner.display_name} is on {self.streaks[uid]} in a row."
                        )

                    points = 1 + bonus + speed_bonus
                    self.scores[uid] = self.scores.get(uid, 0) + points

                    await self.ava_say(
                        f"{winner.mention} {random.choice(self.correct_lines)} (+{points})"
                    )

                    await asyncio.sleep(2)

                await self.send_round_summary(round_num)
                await asyncio.sleep(3)

            await self.end_game()
        except Exception as exc:
            active_games.pop(self.guild_id, None)
            await self.channel.send(f"Something went wrong, so I stopped the game: `{exc}`")
            raise

    async def send_round_summary(self, round_num):
        leaderboard = sorted(self.scores.items(), key=lambda item: item[1], reverse=True)
        if not leaderboard:
            await self.ava_say(f"Round {round_num} complete! No scores yet.")
            return

        lines = await self.build_score_lines(leaderboard)

        embed = ava_embed(
            title=f"Round {round_num} Scores",
            description="\n".join(lines),
            color=AVA_PURPLE if round_num < self.rounds else AVA_GOLD,
        )
        embed.add_field(
            name="Round Progress",
            value=progress_bar(round_num, self.rounds, width=10),
            inline=False,
        )
        embed.set_footer(text="Streaks and speed bonuses can change everything.")
        await self.channel.send(embed=embed)

    # -------------------------
    # END GAME
    # -------------------------

    async def end_game(self):
        sorted_scores = sorted(self.scores.items(), key=lambda item: item[1], reverse=True)

        if not sorted_scores:
            embed = ava_embed(
                title="Final Results",
                description="No scores recorded.",
                color=AVA_PURPLE,
            )
        else:
            lines = await self.build_score_lines(sorted_scores)
            winner_id, winner_score = sorted_scores[0]
            winner_name = await self.player_name(winner_id)
            for index, (user_id, score) in enumerate(sorted_scores, 1):
                update_stats(user_id, self.guild_id, score, index == 1)
            embed = ava_embed(
                title="Final Results",
                description=f"**Winner: {winner_name} with {winner_score} pts**\n\n" + "\n".join(lines),
                color=AVA_GOLD,
            )
            embed.set_footer(text="Leaderboard updated. The realm remembers.")

        await self.channel.send(embed=embed)
        active_games.pop(self.guild_id, None)


class AvaLobbyView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    def get_current_game(self, interaction):
        if interaction.guild is None:
            return None
        game = active_games.get(interaction.guild.id)
        if game is not self.game:
            return None
        return game

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.get_current_game(interaction)
        if not game or game.state != "lobby":
            return await interaction.response.send_message(
                "This lobby is no longer open.",
                ephemeral=True,
            )

        game.join(interaction.user)
        await game.update_lobby()
        await interaction.response.send_message("You're in!", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.get_current_game(interaction)
        if not game:
            return await interaction.response.send_message(
                "This lobby is no longer active.",
                ephemeral=True,
            )

        game.leave(interaction.user)
        await game.update_lobby()
        await interaction.response.send_message("You left the lobby.", ephemeral=True)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.get_current_game(interaction)
        if not game or game.state != "lobby":
            return await interaction.response.send_message(
                "This game has already started or ended.",
                ephemeral=True,
            )
        if interaction.user.id not in game.players:
            return await interaction.response.send_message(
                "Join the lobby before starting it.",
                ephemeral=True,
            )

        game.state = "active"
        await game.update_lobby()
        await interaction.response.send_message("Starting ASOIAF trivia!")
        game.task = asyncio.create_task(game.run_game())

# =========================
# SLASH COMMANDS
# =========================


@bot.tree.command(name="avaasoiaf", description="Create an ASOIAF trivia lobby")
async def avaasoiaf(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    if guild_id in active_games:
        return await interaction.response.send_message(
            "A game or lobby is already running in this server.",
            ephemeral=True,
        )

    game = AvaGame(guild_id, interaction.channel)
    game.join(interaction.user)
    active_games[guild_id] = game

    await interaction.response.send_message(
        "ASOIAF trivia lobby created. I added you as the first player."
    )
    await game.update_lobby()


@bot.tree.command(name="join", description="Join the current Ava trivia lobby")
async def join(interaction: discord.Interaction):
    game = active_games.get(interaction.guild.id)

    if not game:
        return await interaction.response.send_message(
            "There is no active lobby. Use `/avaasoiaf` to create one.",
            ephemeral=True,
        )
    if game.state != "lobby":
        return await interaction.response.send_message(
            "That game has already started. Join the next one!",
            ephemeral=True,
        )

    game.join(interaction.user)
    await game.update_lobby()
    await interaction.response.send_message("You're in!", ephemeral=True)


@bot.tree.command(name="start", description="Start the current Ava trivia lobby")
async def start(interaction: discord.Interaction):
    game = active_games.get(interaction.guild.id)

    if not game:
        return await interaction.response.send_message(
            "There is no active lobby. Use `/avaasoiaf` to create one.",
            ephemeral=True,
        )
    if game.state != "lobby":
        return await interaction.response.send_message(
            "The game has already started.",
            ephemeral=True,
        )
    if interaction.user.id not in game.players:
        return await interaction.response.send_message(
            "Join the lobby with `/join` before starting it.",
            ephemeral=True,
        )

    game.state = "active"
    await game.update_lobby()
    await interaction.response.send_message("Starting ASOIAF trivia!")
    game.task = asyncio.create_task(game.run_game())


@bot.tree.command(name="leave", description="Leave the current Ava trivia game")
async def leave(interaction: discord.Interaction):
    game = active_games.get(interaction.guild.id)

    if not game:
        return await interaction.response.send_message("No active lobby or game.", ephemeral=True)

    game.leave(interaction.user)
    await game.update_lobby()
    await interaction.response.send_message("You left the game.", ephemeral=True)


@bot.tree.command(name="avaforceend", description="Force end the current Ava trivia game")
async def avaforceend(interaction: discord.Interaction):
    game = active_games.get(interaction.guild.id)

    if not game:
        return await interaction.response.send_message("No active Ava game to end.", ephemeral=True)

    can_force_end = (
        interaction.user.id in game.players
        or interaction.user.guild_permissions.manage_guild
        or interaction.user.guild_permissions.administrator
    )
    if not can_force_end:
        return await interaction.response.send_message(
            "Only a player or server manager can force end Ava's game.",
            ephemeral=True,
        )

    game.accepting = False
    game.state = "ended"
    active_games.pop(interaction.guild.id, None)

    if game.task and not game.task.done():
        game.task.cancel()

    await game.update_lobby()

    embed = ava_embed(
        title="Ava Game Ended",
        description=f"The game was force-ended by {interaction.user.mention}.",
        color=AVA_PURPLE,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Show the Ava trivia leaderboard")
async def leaderboard(interaction: discord.Interaction):
    rows = get_leaderboard(interaction.guild.id)

    if not rows:
        return await interaction.response.send_message("No leaderboard scores yet.")

    lines = []

    for index, (user_id, points, wins) in enumerate(rows[:10], 1):
        user = await bot.fetch_user(int(user_id))
        lines.append(f"{index}. {user.name} - {points} pts, {wins} wins")

    embed = ava_embed(
        title="Ava Leaderboard",
        description="\n".join(lines),
        color=AVA_GOLD,
    )

    await interaction.response.send_message(embed=embed)

# =========================
# READY
# =========================


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")


if "DISCORD_TOKEN" not in os.environ:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

delay = 60
while True:
    try:
        bot.run(os.environ["DISCORD_TOKEN"])
        break
    except discord.HTTPException as exc:
        if exc.status != 429:
            raise
        print(
            f"Discord is rate-limiting bot login attempts. "
            f"Waiting {delay} seconds before trying again."
        )
        time.sleep(delay)
        delay = min(delay * 2, 900)
