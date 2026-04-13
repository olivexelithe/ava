import discord
from discord.ext import commands
import asyncio
import json
import random
import sqlite3
from rapidfuzz import fuzz

# =========================
# BOT SETUP
# =========================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

active_games = {}

# =========================
# LOAD QUESTIONS
# =========================

with open("questions.json", "r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)

# =========================
# DATABASE
# =========================

DB_NAME = "ava.db"

def db():
    return sqlite3.connect(DB_NAME)

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS leaderboard (
        user_id TEXT,
        guild_id TEXT,
        points INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        games INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, guild_id)
    )
    """)

    conn.commit()
    conn.close()

init_db()

def update_stats(user_id, guild_id, points=0, win=False):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT OR IGNORE INTO leaderboard (user_id, guild_id, points, wins, games)
    VALUES (?, ?, 0, 0, 0)
    """, (str(user_id), str(guild_id)))

    cur.execute("""
    UPDATE leaderboard
    SET points = points + ?,
        games = games + 1,
        wins = wins + ?
    WHERE user_id = ? AND guild_id = ?
    """, (points, 1 if win else 0, str(user_id), str(guild_id)))

    conn.commit()
    conn.close()

def get_leaderboard(guild_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT user_id, points, wins
    FROM leaderboard
    WHERE guild_id = ?
    ORDER BY points DESC
    """, (str(guild_id),))

    rows = cur.fetchall()
    conn.close()
    return rows

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

        self.accepting = False
        self.current_answer = None

        self.state = "lobby"
        self.topic = None
        self.rounds = 6

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
        players = "\n".join([f"<@{p}>" for p in self.players]) or "No players yet"

        embed = discord.Embed(
            title="🎮 AVA TRIVIA LOBBY",
            description=(
                "Welcome!\n\n"
                "• Use `/avaasoiaf` to start\n"
                "• Answer questions quickly\n"
            ),
            color=discord.Color.blurple()
        )

        embed.add_field(name="👥 Players", value=players, inline=False)
        embed.add_field(name="📊 Status", value=self.state.upper(), inline=False)

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

    def get_timer(self, i):
        if i < self.rounds * 0.33:
            return 25
        elif i < self.rounds * 0.66:
            return 20
        return 15

    def is_correct(self, message):
        if not self.accepting:
            return False
        if message.author.id not in self.players:
            return False

        guess = message.content.lower().strip()
        answer = self.current_answer.lower().strip()

        return fuzz.ratio(guess, answer) >= 80

    def streak_bonus(self, streak):
        if streak >= 7:
            return 3
        if streak >= 5:
            return 2
        if streak >= 3:
            return 1
        return 0

    # -------------------------
    # GAME LOOP
    # -------------------------

    async def run_game(self):
        questions = QUESTIONS[self.topic]
        random.shuffle(questions)

        for i in range(self.rounds):

            q = questions[i % len(questions)]
            self.current_answer = q["answer"]
            self.accepting = True

            timer = self.get_timer(i)

            await self.channel.send(
                f"❓ **Round {i+1}**\n{q['question']}\n⏱️ {timer}s"
            )

            try:
                msg = await bot.wait_for(
                    "message",
                    timeout=timer,
                    check=self.is_correct
                )
                winner = msg.author

            except asyncio.TimeoutError:
                await self.channel.send("⏰ No correct answer.")
                self.accepting = False
                continue

            self.accepting = False

            uid = winner.id

            self.streaks[uid] += 1
            bonus = self.streak_bonus(self.streaks[uid])

            for pid in self.players:
                if pid != uid:
                    self.streaks[pid] = 0

            points = 1 + bonus
            self.scores[uid] = self.scores.get(uid, 0) + points

            await self.channel.send(f"✅ {winner.mention} +{points}")
            await asyncio.sleep(2)

        await self.end_game()

    async def end_game(self):
        sorted_scores = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)

        msg = "🏁 **FINAL RESULTS**\n"

        for i, (uid, score) in enumerate(sorted_scores):
            user = await bot.fetch_user(uid)
            msg += f"{i+1}. {user.name} — {score}\n"
            update_stats(uid, self.guild_id, score, i == 0)

        await self.channel.send(msg)
        active_games.pop(self.guild_id, None)

# =========================
# SLASH COMMANDS
# =========================

@bot.tree.command(name="avaasoiaf", description="Start ASOIAF trivia")
async def avaasoiaf(interaction: discord.Interaction):

    guild_id = interaction.guild.id

    if guild_id in active_games:
        return await interaction.response.send_message("Game already running.", ephemeral=True)

    game = AvaGame(guild_id, interaction.channel)
    active_games[guild_id] = game

    game.topic = "asoiaf"
    game.state = "active"

    await interaction.response.send_message("🔥 ASOIAF Trivia starting!")
    await game.run_game()

# -------------------------
# LEAVE COMMAND (FIXED)
# -------------------------

@bot.tree.command(name="leave", description="Leave game")
async def leave(interaction: discord.Interaction):

    game = active_games.get(interaction.guild.id)

    if not game:
        return await interaction.response.send_message("No active lobby.", ephemeral=True)

    game.leave(interaction.user)
    await game.update_lobby()

    await interaction.response.send_message("👋 You left the lobby.", ephemeral=True)

# =========================
# LEADERBOARD
# =========================

@bot.tree.command(name="leaderboard", description="Leaderboard")
async def leaderboard(interaction: discord.Interaction):

    rows = get_leaderboard(interaction.guild.id)

    msg = "🌍 **LEADERBOARD**\n"

    for i, (uid, points, wins) in enumerate(rows[:10]):
        user = await bot.fetch_user(int(uid))
        msg += f"{i+1}. {user.name} — {points} pts\n"

    await interaction.response.send_message(msg)

# =========================
# READY
# =========================

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

bot.run(os.environ["DISCORD_TOKEN"])
