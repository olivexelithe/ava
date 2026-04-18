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

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
active_games = {}

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

        embed = discord.Embed(
            title="AVA TRIVIA LOBBY",
            description=(
                "ASOIAF trivia is ready.\n\n"
                "Use `/join` to enter the game.\n"
                "Use `/start` when everyone is ready.\n"
                "Use `/leave` if you need to drop out."
            ),
            color=discord.Color.blurple(),
        )

        embed.add_field(name="Players", value=players, inline=False)
        embed.add_field(name="Status", value=self.state.upper(), inline=False)
        embed.add_field(
            name="Format",
            value=f"{self.rounds} rounds, {self.questions_per_round} questions per round",
            inline=False,
        )

        return embed

    async def update_lobby(self):
        embed = self.build_lobby_embed()

        if self.lobby_message:
            await self.lobby_message.edit(embed=embed)
        else:
            self.lobby_message = await self.channel.send(embed=embed)

    # -------------------------
    # GAME LOGIC
    # -------------------------

    def get_timer(self, round_num):
        if round_num <= self.rounds * 0.33:
            return 25
        if round_num <= self.rounds * 0.66:
            return 20
        return 15

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
        await self.channel.send(f"**Ava:** {message}")

    async def player_name(self, user_id):
        member = self.channel.guild.get_member(user_id)
        if member:
            return member.display_name
        user = await bot.fetch_user(user_id)
        return user.name

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

                await self.ava_say(f"Round {round_num} starting in 5 seconds...")
                await asyncio.sleep(5)
                await self.ava_say(f"{self.questions_per_round} questions this round. Let's go!")

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
                        f"**Round {round_num} - Question {q_num}/{self.questions_per_round}**\n"
                        f"{q['question']}\n"
                        f"Time: {timer}s"
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
                        await self.ava_say(f"SPEED BONUS for {winner.display_name}!")

                    if self.streaks[uid] >= 3:
                        await self.ava_say(
                            f"{winner.display_name} is on a {self.streaks[uid]} streak!"
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
        await self.ava_say(f"Round {round_num} complete! Scores:")

        leaderboard = sorted(self.scores.items(), key=lambda item: item[1], reverse=True)
        if not leaderboard:
            await self.channel.send("No scores yet.")
            return

        lines = []
        for index, (player_id, score) in enumerate(leaderboard, 1):
            name = await self.player_name(player_id)
            lines.append(f"{index}. {name} - {score} pts")

        await self.channel.send("\n".join(lines))

    # -------------------------
    # END GAME
    # -------------------------

    async def end_game(self):
        sorted_scores = sorted(self.scores.items(), key=lambda item: item[1], reverse=True)

        msg = "**FINAL RESULTS**\n"

        if not sorted_scores:
            msg += "No scores recorded."
        else:
            for index, (user_id, score) in enumerate(sorted_scores, 1):
                name = await self.player_name(user_id)
                msg += f"{index}. {name} - {score} pts\n"
                update_stats(user_id, self.guild_id, score, index == 1)

        await self.channel.send(msg)
        active_games.pop(self.guild_id, None)

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


@bot.tree.command(name="leaderboard", description="Show the Ava trivia leaderboard")
async def leaderboard(interaction: discord.Interaction):
    rows = get_leaderboard(interaction.guild.id)

    if not rows:
        return await interaction.response.send_message("No leaderboard scores yet.")

    msg = "**LEADERBOARD**\n"

    for index, (user_id, points, wins) in enumerate(rows[:10], 1):
        user = await bot.fetch_user(int(user_id))
        msg += f"{index}. {user.name} - {points} pts, {wins} wins\n"

    await interaction.response.send_message(msg)

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
