"""Esports commands - schedule, live scores, results, follow-a-team auto-post.

All from Henrik's esports schedule (one endpoint, VLR/lolesports data), keyed by
nothing personal. Needs HENRIK_API_KEY.

Covered: upcoming fixtures, live scores, recent results, and per-USER team
follows - each person follows their own teams and gets a private DM when one of
their teams' matches finishes (esports_follows.json = {discord_id: {teams, seen}}).
Not covered: VLR team/player detail pages - those need a numeric VLR team_id the
free API has no name-search for, so they're impractical here.

Wire in:  import esports; esports.register(bot)   (+ esports.start_jobs() in setup_hook)
"""

import datetime
import json
import os
import pathlib
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

BASE = "https://api.henrikdev.xyz"
RED = 0xFF4655
API_KEY = os.getenv("HENRIK_API_KEY")
FOLLOWS_FILE = pathlib.Path(__file__).with_name("esports_follows.json")

_bot = None
_cache = {"at": 0.0, "data": []}  # 60s schedule cache — shared by cmds + autocomplete + job


# --------------------------------------------------------------------------
# storage - single-server config
# --------------------------------------------------------------------------

def load_follows() -> dict:
    try:
        with open(FOLLOWS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_follows(d: dict) -> None:
    tmp = FOLLOWS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, FOLLOWS_FILE)


# --------------------------------------------------------------------------
# pure helpers - unit tested
# --------------------------------------------------------------------------

def match_teams(m: dict) -> list[str]:
    return [t.get("name", "?") for t in (m.get("match") or {}).get("teams") or []]


def league_of(m: dict) -> str:
    return (m.get("league") or {}).get("name", "")


def match_ts(m: dict) -> int:
    """ISO date -> unix seconds. 0 if unparseable (sorts such matches first)."""
    try:
        return int(datetime.datetime.fromisoformat(m["date"]).timestamp())
    except (KeyError, ValueError, TypeError):
        return 0


def filter_matches(matches: list, state=None, league=None) -> list:
    """Filter by state ('unstarted'/'in_progress'/'completed') and a league
    substring (case-insensitive), sorted by date."""
    out = matches
    if state:
        states = {state} if isinstance(state, str) else set(state)
        out = [m for m in out if m.get("state") in states]
    if league:
        lq = league.lower()
        out = [m for m in out if lq in league_of(m).lower()
               or lq in (m.get("league") or {}).get("identifier", "").lower()]
    return sorted(out, key=match_ts)


def new_results_for(matches: list, teams: list[str], seen: list[str]) -> list:
    """Completed matches involving a followed team that we haven't posted yet.
    Team match is case-insensitive substring so 'sentinels' catches 'Sentinels'."""
    tl = [t.lower() for t in teams]
    seenset = set(seen)
    out = []
    for m in matches:
        if m.get("state") != "completed":
            continue
        mid = (m.get("match") or {}).get("id")
        if not mid or mid in seenset:
            continue
        names = " ".join(match_teams(m)).lower()
        if any(t in names for t in tl):
            out.append(m)
    return out


def fmt_score(m: dict) -> str:
    """'**Sentinels** 2–0 GIANTX' with the winner bold."""
    teams = (m.get("match") or {}).get("teams") or []
    if len(teams) < 2:
        return " vs ".join(match_teams(m)) or "TBD"
    a, b = teams[0], teams[1]
    an = f"**{a.get('name','?')}**" if a.get("has_won") else a.get("name", "?")
    bn = f"**{b.get('name','?')}**" if b.get("has_won") else b.get("name", "?")
    return f"{an} {a.get('game_wins',0)}–{b.get('game_wins',0)} {bn}"


def fmt_upcoming(m: dict) -> str:
    names = match_teams(m)
    matchup = " vs ".join(names) if len(names) == 2 else "TBD"
    live = " 🔴 **LIVE**" if m.get("state") == "in_progress" else ""
    when = f"<t:{match_ts(m)}:R>" if match_ts(m) else "TBD"
    return f"{when}{live} — {matchup}  ·  *{league_of(m)}*"


# --------------------------------------------------------------------------
# fetch (cached)
# --------------------------------------------------------------------------

async def fetch_schedule(session) -> list:
    if not API_KEY:
        return []
    if time.time() - _cache["at"] < 60 and _cache["data"]:
        return _cache["data"]
    try:
        async with session.get(f"{BASE}/valorant/v1/esports/schedule",
                               headers={"Authorization": API_KEY}) as r:
            if r.status != 200:
                return _cache["data"]
            data = (await r.json()).get("data") or []
    except aiohttp.ClientError:
        return _cache["data"]
    _cache["at"], _cache["data"] = time.time(), data
    return data


# --------------------------------------------------------------------------
# auto-post job
# --------------------------------------------------------------------------

@tasks.loop(minutes=30)
async def results_job():
    """DM each follower the fresh results for THEIR teams. Per-user follows and
    per-user seen lists, so everyone gets their own teams once."""
    cfg = load_follows()  # {discord_id: {teams: [...], seen: [...]}}
    if not cfg:
        return
    matches = await fetch_schedule(_bot.session)
    if not matches:
        return
    changed = False
    for uid, entry in cfg.items():
        if not isinstance(entry, dict):  # ignore any pre-per-user leftover keys
            continue
        teams = entry.get("teams") or []
        if not teams:
            continue
        fresh = new_results_for(matches, teams, entry.get("seen") or [])
        if not fresh:
            continue
        try:
            user = await _bot.fetch_user(int(uid))
        except (discord.NotFound, discord.HTTPException):
            continue
        seen = set(entry.get("seen") or [])
        for m in fresh:
            e = discord.Embed(title="Result", description=fmt_score(m),
                              color=RED).set_footer(text=league_of(m))
            try:
                await user.send(embed=e)
                seen.add((m.get("match") or {}).get("id"))
            except (discord.Forbidden, discord.HTTPException):
                pass  # DMs closed / left server - skip, don't crash the job
        entry["seen"] = list(seen)[-500:]
        changed = True
    if changed:
        save_follows(cfg)


@results_job.before_loop
async def _wait():
    await _bot.wait_until_ready()


def start_jobs():
    if not results_job.is_running():
        results_job.start()


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def register(bot):
    global _bot
    _bot = bot

    async def need_key(interaction):
        if not API_KEY:
            await interaction.response.send_message(
                "Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.", ephemeral=True)
            return True
        return False

    async def team_autocomplete(interaction, current):
        matches = await fetch_schedule(bot.session)
        names = sorted({n for m in matches for n in match_teams(m) if n and n != "?"})
        cur = current.lower()
        hits = [n for n in names if cur in n.lower()][:25]
        return [app_commands.Choice(name=n, value=n) for n in hits]

    @bot.tree.command(description="Upcoming & live esports matches")
    @app_commands.describe(league="Filter by league, e.g. Americas, EMEA, Pacific, Game Changers")
    async def esports(interaction: discord.Interaction, league: str = None):
        if await need_key(interaction):
            return
        await interaction.response.defer()
        matches = filter_matches(await fetch_schedule(bot.session),
                                 state=["unstarted", "in_progress"], league=league)[:10]
        if not matches:
            await interaction.followup.send("No upcoming matches found" + (f" for '{league}'." if league else "."))
            return
        e = discord.Embed(title="🗓️ Upcoming matches" + (f" · {league}" if league else ""),
                          description="\n".join(fmt_upcoming(m) for m in matches), color=RED)
        await interaction.followup.send(embed=e)

    @bot.tree.command(description="Recent esports results & scores")
    @app_commands.describe(league="Filter by league")
    async def results(interaction: discord.Interaction, league: str = None):
        if await need_key(interaction):
            return
        await interaction.response.defer()
        done = filter_matches(await fetch_schedule(bot.session), state="completed", league=league)
        recent = list(reversed(done))[:10]
        if not recent:
            await interaction.followup.send("No recent results found.")
            return
        lines = [f"{fmt_score(m)}  ·  *{league_of(m)}*" for m in recent]
        await interaction.followup.send(
            embed=discord.Embed(title="✅ Recent results", description="\n".join(lines), color=RED))

    @bot.tree.command(description="Matches live right now")
    async def live(interaction: discord.Interaction):
        if await need_key(interaction):
            return
        await interaction.response.defer()
        now = filter_matches(await fetch_schedule(bot.session), state="in_progress")
        if not now:
            await interaction.followup.send("Nothing live right now.")
            return
        lines = [f"🔴 {fmt_score(m)}  ·  *{league_of(m)}*" for m in now]
        await interaction.followup.send(
            embed=discord.Embed(title="🔴 Live now", description="\n".join(lines), color=RED))

    @bot.tree.command(name="follow-team", description="DM me when a team's match finishes")
    @app_commands.describe(team="Team to follow")
    @app_commands.autocomplete(team=team_autocomplete)
    async def follow_team(interaction: discord.Interaction, team: str):
        if await need_key(interaction):
            return
        cfg = load_follows()
        entry = cfg.setdefault(str(interaction.user.id), {"teams": [], "seen": []})
        if any(t.lower() == team.lower() for t in entry["teams"]):
            await interaction.response.send_message(f"You already follow **{team}**.", ephemeral=True)
            return
        entry["teams"].append(team)
        save_follows(cfg)
        await interaction.response.send_message(
            f"Following **{team}** — I'll DM you when their matches finish.\n"
            "-# If the DM never arrives, allow DMs from server members in your privacy settings.",
            ephemeral=True)

    @bot.tree.command(name="unfollow-team", description="Stop following a team")
    @app_commands.autocomplete(team=team_autocomplete)
    async def unfollow_team(interaction: discord.Interaction, team: str):
        cfg = load_follows()
        entry = cfg.get(str(interaction.user.id)) or {}
        teams = entry.get("teams") or []
        kept = [t for t in teams if t.lower() != team.lower()]
        if len(kept) == len(teams):
            await interaction.response.send_message(f"You weren't following **{team}**.", ephemeral=True)
            return
        entry["teams"] = kept
        save_follows(cfg)
        await interaction.response.send_message(f"Unfollowed **{team}**.", ephemeral=True)

    @bot.tree.command(description="List the teams you follow")
    async def follows(interaction: discord.Interaction):
        entry = load_follows().get(str(interaction.user.id)) or {}
        teams = entry.get("teams") or []
        if not teams:
            await interaction.response.send_message(
                "You don't follow any teams. Add one with `/follow-team`.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**You follow:** {', '.join(teams)}", ephemeral=True)
