"""Premier team lookup (/premier) and regional standings (/premier-standings).

Henrik Premier endpoints (verified live 2026-08):
  GET {BASE}/valorant/v1/premier/{name}/{tag}         -> full team (roster, stats, placement)
  GET {BASE}/valorant/v1/premier/{id}                 -> same, by id
  GET {BASE}/valorant/v1/premier/search?name=<exact>  -> [{id,name,tag,division,conference}]
  GET {BASE}/valorant/v1/premier/leaderboard/{region} -> [team...] ranked by score

Structure (researched + verified live 2026-08):
  - 6 regions: ap, na, eu, kr, latam, br (henrik.REGIONS).
  - each region holds several conferences ("leagues"), 33 in total, listed by
    /valorant/v1/premier/conferences. Every conference has a _SUPER variant that
    holds the Invite-division teams.
  - divisions are ints 1-22 -> named rank tiers. Open/Intermediate/Advanced/Elite
    each split into 5 (divisions 1-20); Contender = 21, Invite = 22. Confirmed:
    only division 22 shows up in the *_SUPER conferences.

Notes from probing:
  - search matches the EXACT full team name only (partials -> []); ?text= is
    fuzzy but floods 30k+ hits, so we don't use it.
  - there is no future-schedule endpoint (history is past matches only), so
    there are no match reminders to build.

Wire in:  import premier; premier.register(bot)
"""

import time

import discord
from discord import app_commands

import henrik

RED = henrik.RED
PLAYOFF_THRESHOLD = 600  # Premier Score needed to reach the playoff bracket

# Open/Intermediate/Advanced/Elite each span 5 divisions (1-20); 21/22 are single
_DIV_TIERS = ["Open", "Intermediate", "Advanced", "Elite"]

# conferences are static-ish -> cache the whole list for the process (refresh 1h)
_CONF_TTL = 3600
_conf_cache: dict = {"at": 0.0, "data": []}


# --------------------------------------------------------------------------
# pure helpers - unit tested
# --------------------------------------------------------------------------

def playoff_progress(points, threshold: int = PLAYOFF_THRESHOLD) -> dict:
    """Premier Score -> {qualified, line}. Below: '540 / 600 — 60 to go';
    at/above: '600 / 600 — playoff threshold reached'."""
    points = points or 0
    if points >= threshold:
        return {"qualified": True, "line": f"{points} / {threshold} — playoff threshold reached"}
    return {"qualified": False, "line": f"{points} / {threshold} — {threshold - points} to go"}


def division_name(div) -> str:
    """Premier division int (1-22) -> named rank tier, e.g. 3 -> 'Open 3',
    16 -> 'Elite 1', 21 -> 'Contender', 22 -> 'Invite'. 0/None -> 'Unrated'."""
    if not div or div < 1:
        return "Unrated"
    if div >= 22:
        return "Invite"
    if div == 21:
        return "Contender"
    return f"{_DIV_TIERS[(div - 1) // 5]} {(div - 1) % 5 + 1}"


def pretty_conference(name: str) -> str:
    """'AP_OCEANIA_SUPER' -> 'AP Oceania (Super)'. Keeps the region code upper,
    title-cases the league, flags the Super (Invite) conference."""
    if not name:
        return "?"
    parts = name.split("_")
    is_super = parts[-1] == "SUPER"
    if is_super:
        parts = parts[:-1]
    label = f"{parts[0]} " + " ".join(p.capitalize() for p in parts[1:])
    return f"{label.strip()} (Super)" if is_super else label.strip()


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def register(bot):
    async def get(path, **kw):
        """Henrik GET -> (status, json). (0, None) if no key set."""
        if not henrik.API_KEY:
            return 0, None
        async with bot.session.get(f"{henrik.BASE}{path}",
                                   headers={"Authorization": henrik.API_KEY}, **kw) as r:
            return r.status, await r.json()

    region_choices = [app_commands.Choice(name=r.upper(), value=r) for r in henrik.REGIONS]

    async def _find_team(name, tag):
        """Resolve a team -> (status, json). With a tag: exact name/tag lookup.
        Without: exact-name search, then fetch the single hit by id."""
        if tag:
            return await get(f"/valorant/v1/premier/{name}/{tag.lstrip('#')}")
        status, data = await get("/valorant/v1/premier/search", params={"name": name})
        hits = ((data or {}).get("data") or []) if status == 200 else []
        if not hits:
            return 404, None
        return await get(f"/valorant/v1/premier/{hits[0]['id']}")

    @bot.tree.command(description="Premier team — roster, division, W-L, playoff progress")
    @app_commands.describe(name="Exact team name", tag="Team tag (omit to search by exact name)")
    async def premier(interaction: discord.Interaction, name: str, tag: str = None):
        await interaction.response.defer()
        if not henrik.API_KEY:
            await interaction.followup.send("Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.")
            return
        status, data = await _find_team(name, tag)
        if status == 404 or not (data and data.get("data")):
            await interaction.followup.send(
                f"No Premier team found for **{name}**"
                + (f"#{tag.lstrip('#')}" if tag else "")
                + ". Use the exact team name, or add the team tag.")
            return
        if status != 200:
            await interaction.followup.send("Couldn't reach Henrik. Try again later.")
            return
        d = data["data"]
        st = d.get("stats") or {}
        place = d.get("placement") or {}
        prog = playoff_progress(place.get("points"))
        members = d.get("member") or []
        roster = ", ".join(f"{m.get('name')}#{m.get('tag')}" for m in members)
        e = discord.Embed(title=f"{d.get('name')} #{d.get('tag')}", color=RED)
        e.add_field(name="Division",
                    value=f"**{division_name(place.get('division'))}** · "
                          f"{pretty_conference(place.get('conference'))}",
                    inline=False)
        e.add_field(name="Record",
                    value=f"{st.get('wins', 0)}-{st.get('losses', 0)}  ({st.get('matches', 0)} played)")
        e.add_field(name="Premier Score",
                    value=("✅ " if prog["qualified"] else "") + prog["line"])
        if roster:
            e.add_field(name=f"Roster ({len(members)})", value=roster[:1024], inline=False)
        img = (d.get("customization") or {}).get("image")
        if img:
            e.set_thumbnail(url=img)
        await interaction.followup.send(embed=e)

    @bot.tree.command(name="premier-standings", description="Top Premier teams in a region / league")
    @app_commands.describe(region="Region (default AP)",
                           conference="Filter to a league, e.g. 'oceania' or 'us east' (see /premier-conferences)")
    @app_commands.choices(region=region_choices)
    async def premier_standings(interaction: discord.Interaction,
                                region: str = henrik.DEFAULT_REGION,
                                conference: str = None):
        await interaction.response.defer()
        if not henrik.API_KEY:
            await interaction.followup.send("Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.")
            return
        reg = henrik.valid_region(region)
        status, data = await get(f"/valorant/v1/premier/leaderboard/{reg}")
        teams = ((data or {}).get("data") or []) if status == 200 else []
        if conference:
            q = conference.lower().replace(" ", "").replace("_", "")
            teams = [t for t in teams if q in (t.get("conference") or "").lower().replace("_", "")]
        if not teams:
            where = f"{reg.upper()} · {conference}" if conference else reg.upper()
            await interaction.followup.send(f"No Premier teams found for **{where}**.")
            return
        lines = [
            f"`{i:>2}` **{t.get('name')}**#{t.get('tag')} · {division_name(t.get('division'))} · "
            f"{pretty_conference(t.get('conference'))} · "
            f"{t.get('wins', 0)}-{t.get('losses', 0)} · {t.get('score', 0)} pts"
            for i, t in enumerate(teams[:15], 1)]
        title = f"{reg.upper()} Premier — top teams" + (f" · {conference}" if conference else "")
        e = discord.Embed(title=title, description="\n".join(lines)[:4096], color=RED)
        await interaction.followup.send(embed=e)

    async def _fetch_conferences():
        """All Premier conferences (33), cached ~1h — static-ish data."""
        if time.time() - _conf_cache["at"] < _CONF_TTL and _conf_cache["data"]:
            return _conf_cache["data"]
        status, data = await get("/valorant/v1/premier/conferences")
        if status == 200 and data and data.get("data"):
            _conf_cache["at"], _conf_cache["data"] = time.time(), data["data"]
        return _conf_cache["data"]

    @bot.tree.command(name="premier-conferences", description="List the Premier leagues (conferences) in a region")
    @app_commands.describe(region="Region (default AP)")
    @app_commands.choices(region=region_choices)
    async def premier_conferences(interaction: discord.Interaction,
                                  region: str = henrik.DEFAULT_REGION):
        await interaction.response.defer()
        if not henrik.API_KEY:
            await interaction.followup.send("Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.")
            return
        reg = henrik.valid_region(region)
        confs = await _fetch_conferences()
        # skip the _SUPER duplicates - one line per base league, with its cities
        rows = [c for c in confs if c.get("region") == reg and not (c.get("name") or "").endswith("_SUPER")]
        if not rows:
            await interaction.followup.send(f"No Premier conferences listed for {reg.upper()}.")
            return
        lines = []
        for c in rows:
            cities = ", ".join(p.get("name") for p in (c.get("pods") or []) if p.get("name"))
            lines.append(f"• **{pretty_conference(c.get('name'))}** — {cities or c.get('timezone', '')}")
        e = discord.Embed(
            title=f"{reg.upper()} Premier leagues",
            description="\n".join(lines) + "\n\n-# Use the league name with `/premier-standings`.",
            color=RED)
        await interaction.followup.send(embed=e)
