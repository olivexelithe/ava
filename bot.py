import discord
from discord.ext import commands
import asyncio
import json
import random
import os
import sqlite3
from rapidfuzz import fuzz

# =========================
# BOT SETUP (SLASH COMMANDS)
# =========================

intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)

BOT_NAME = "AVA 🤖"

active_games = {}

# =========================
# LOAD QUESTIONS
# =========================

with open("questions.json", "r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)

# =========================
# SQLITE DATABASE
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

def add_player(user_id, guild_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT OR IGNORE INTO leaderboard (user_id, guild_id, points, wins, games)
    VALUES (?, ?, 0, 0, 0)
    """, (str(user_id), str(guild_id)))

    conn.commit()
    conn.close()

def update_stats(user_id, guild_id, points=0, win=False):
    conn = db()
    cur = conn.cursor()

    add_player(user_id, guild_id)

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

        self.accepting = False
        self.current_answer = None

        # NEW
        self.state = "lobby"  # lobby → waiting_topic → active
        self.topic = None
        self.rounds = 6
        self.lobby_task = None

    # ---------------------
    # JOIN
    # ---------------------
    def join(self, user):
        self.players.add(user.id)
        self.scores.setdefault(user.id, 0)
        self.streaks.setdefault(user.id, 0)

    async def start_lobby_timer(self):
    await asyncio.sleep(300)  # 5 minutes

    if len(self.players) >= 3 and self.state == "lobby":
        self.state = "waiting_topic"

        await self.channel.send(
            "🎮 Which trivia subject are you wanting to play?\n\n"
            f"Available: {list(QUESTIONS.keys())}\n"
            "Use `/ava topic <name>`"
        )
    else:
        await self.channel.send("❌ Not enough players joined in time.")
        active_games.pop(self.guild_id, None)

    # ---------------------
    # TIMER
    # ---------------------
    def get_timer(self, i):
        if i < self.rounds * 0.33:
            return 30
        elif i < self.rounds * 0.66:
            return 20
        return 15

    # ---------------------
    # CHECK ANSWER
    # ---------------------
    def is_correct(self, message):
        if not self.accepting:
            return False

        if message.author.id not in self.players:
            return False

        guess = message.content.lower().strip()
        answer = self.current_answer.lower().strip()

        return fuzz.ratio(guess, answer) >= 80

    # ---------------------
    # STREAK BONUS
    # ---------------------
    def streak_bonus(self, streak):
        if streak >= 7:
            return 3
        if streak >= 5:
            return 2
        if streak >= 3:
            return 1
        return 0

    # ---------------------
    # GAME LOOP
    # ---------------------
    async def run_game(self):

        questions = QUESTIONS[self.topic]
        random.shuffle(questions)

        round_types = ["classic", "blitz", "chaos"]

        for i in range(self.rounds):

            q = questions[i % len(questions)]
            self.current_answer = q["answer"]
            self.accepting = True

            round_type = random.choice(round_types)
            timer = self.get_timer(i)

            if round_type == "blitz":
                timer = max(10, timer - 10)

            await self.channel.send(
                f"❓ **Round {i+1} [{round_type.upper()}]**\n"
                f"{q['question']}\n"
                f"⏱️ {timer} seconds"
            )

            def check(m):
                return self.is_correct(m)

            try:
                msg = await bot.wait_for("message", timeout=timer, check=check)
                winner = msg.author
            except asyncio.TimeoutError:
                await self.channel.send("⏰ Nobody got it.")
                self.accepting = False
                continue

            self.accepting = False

            uid = winner.id

            self.streaks[uid] += 1
            streak_bonus = self.streak_bonus(self.streaks[uid])

            for pid in self.players:
                if pid != uid:
                    self.streaks[pid] = 0

            base = 1
            speed = 1
            multiplier = 2 if round_type == "blitz" else 1

            total = (base * multiplier) + speed + streak_bonus

            self.scores[uid] += total

            await self.channel.send(
                f"🎉 {winner.mention} correct!\n"
                f"+{total} points"
            )

            await asyncio.sleep(2)

        await self.end_game()

    # ---------------------
    # END GAME
    # ---------------------
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

@bot.event
async def on_ready():
    print(f"AVA ONLINE as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(e)

@bot.tree.command(name="ava", description="Main Ava command")
async def ava(interaction: discord.Interaction, action: str, arg: str = None):

    guild_id = interaction.guild.id
    game = active_games.get(guild_id)

    # ------------------
    # START LOBBY
    # ------------------
    if action == "start":

        if game:
            return await interaction.response.send_message(
                "⚠️ Game already running.", ephemeral=True
            )

        game = AvaGame(guild_id, interaction.channel)
        active_games[guild_id] = game

        game.lobby_task = asyncio.create_task(game.start_lobby_timer())

        await interaction.response.send_message(
            "🎮 Lobby started!\nUse `/ava join` to enter.\n"
            "Game will prompt for topic in 5 minutes."
        )

    # ------------------
    # JOIN
    # ------------------
    elif action == "join":

        if not game:
            return await interaction.response.send_message(
                "❌ No lobby active.", ephemeral=True
            )

        if game.state != "lobby":
            return await interaction.response.send_message(
                "❌ You can't join right now.", ephemeral=True
            )

        game.join(interaction.user)

        await interaction.response.send_message(
            f"✅ {interaction.user.name} joined! ({len(game.players)}/3+)"
        )

    # ------------------
    # TOPIC SELECT
    # ------------------
    elif action == "topic":

        if not game or game.state != "waiting_topic":
            return await interaction.response.send_message(
                "❌ Not choosing a topic right now.", ephemeral=True
            )

        if not arg or arg not in QUESTIONS:
            return await interaction.response.send_message(
                f"❌ Available topics: {list(QUESTIONS.keys())}",
                ephemeral=True
            )

        game.topic = arg
        game.state = "active"

        if game.lobby_task:
            game.lobby_task.cancel()

        await interaction.response.send_message(
            f"🔥 Starting **{arg}** trivia!"
        )

        await game.run_game()

    # ------------------
    # STATUS
    # ------------------
    elif action == "status":

        if not game:
            return await interaction.response.send_message("No active game.")

        await interaction.response.send_message(
            f"State: {game.state}\nPlayers: {len(game.players)}"
        )

    else:
        await interaction.response.send_message(
            "❓ Commands:\n"
            "`/ava start`\n"
            "`/ava join`\n"
            "`/ava topic <name>`\n"
            "`/ava status`"
        )

# -------------------------
# /leaderboard
# -------------------------
@bot.tree.command(name="leaderboard", description="View server leaderboard")
async def leaderboard(interaction: discord.Interaction):

    rows = get_leaderboard(interaction.guild.id)

    if not rows:
        return await interaction.response.send_message("No data yet.")

    msg = "🌍 **AVA LEADERBOARD**\n"

    for i, (uid, points, wins) in enumerate(rows[:10]):
        user = await bot.fetch_user(int(uid))
        msg += f"{i+1}. {user.name} — {points} pts | 🏆 {wins} wins\n"

    await interaction.response.send_message(msg)

# =========================
# MESSAGE LISTENER (ANSWERS)
# =========================

@bot.event
async def on_message(message):
    await bot.process_commands(message)

    if message.author.bot:
        return

    game = active_games.get(message.guild.id) if message.guild else None
    if not game:
        return

    if game.state == "active" and game.is_correct(message):
    game.accepting = False

# =========================
# RUN BOT
# =========================

bot.run(os.environ["DISCORD_TOKEN"])
