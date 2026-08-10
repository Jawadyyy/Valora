"""Premier team lookup (/premier) and regional standings (/premier-standings).

Henrik Premier endpoints (verified live 2026-08):
  GET {BASE}/valorant/v1/premier/{name}/{tag}         -> full team (roster, stats, placement)
  GET {BASE}/valorant/v1/premier/{id}                 -> same, by id
  GET {BASE}/valorant/v1/premier/search?name=<exact>  -> [{id,name,tag,division,conference}]
  GET {BASE}/valorant/v1/premier/leaderboard/{region} -> [team...] ranked by score

Notes from probing:
  - search matches the EXACT full team name only (partials -> []); ?text= is
    fuzzy but floods 30k+ hits, so we don't use it.
  - there is no future-schedule endpoint (history is past matches only), so
    there are no match reminders to build.

Wire in:  import premier; premier.register(bot)
"""

import discord
from discord import app_commands

import henrik

RED = henrik.RED
PLAYOFF_THRESHOLD = 625  # Premier Score needed to reach the playoff bracket


# --------------------------------------------------------------------------
# pure helpers - unit tested
# --------------------------------------------------------------------------

def playoff_progress(points, threshold: int = PLAYOFF_THRESHOLD) -> dict:
    """Premier Score -> {qualified, line}. Below: '540 / 625 — 85 to go';
    at/above: '625 / 625 — playoff threshold reached'."""
    points = points or 0
    if points >= threshold:
        return {"qualified": True, "line": f"{points} / {threshold} — playoff threshold reached"}
    return {"qualified": False, "line": f"{points} / {threshold} — {threshold - points} to go"}


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
                    value=f"Div {place.get('division', '?')} · {place.get('conference', '?')}",
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

    @bot.tree.command(name="premier-standings", description="Top Premier teams in a region")
    @app_commands.describe(region="Region (default AP)")
    @app_commands.choices(region=region_choices)
    async def premier_standings(interaction: discord.Interaction,
                                region: str = henrik.DEFAULT_REGION):
        await interaction.response.defer()
        if not henrik.API_KEY:
            await interaction.followup.send("Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.")
            return
        reg = henrik.valid_region(region)
        status, data = await get(f"/valorant/v1/premier/leaderboard/{reg}")
        teams = ((data or {}).get("data") or []) if status == 200 else []
        if not teams:
            await interaction.followup.send(f"No Premier standings for {reg.upper()} right now.")
            return
        lines = [
            f"`{i:>2}` **{t.get('name')}**#{t.get('tag')} · Div {t.get('division', '?')} · "
            f"{t.get('wins', 0)}-{t.get('losses', 0)} · {t.get('score', 0)} pts"
            for i, t in enumerate(teams[:15], 1)]
        e = discord.Embed(title=f"{reg.upper()} Premier — top teams",
                          description="\n".join(lines), color=RED)
        await interaction.followup.send(embed=e)
