"""Per-game history (/matches) and RR-over-time (/mmr-history).

Both default to the caller's /link-riot identity (stored in rankroles.json) and
accept a name/tag/region override. Henrik-keyed, no cookie - see henrik.py.

Wire in from the main file:  import stats; stats.register(bot)
"""

from itertools import accumulate

import discord
from discord import app_commands

import henrik
import rankroles

RED = henrik.RED
BARS = "▁▂▃▄▅▆▇█"


# --------------------------------------------------------------------------
# pure helpers - unit tested
# --------------------------------------------------------------------------

def format_match(m: dict) -> dict:
    """One stored match -> flat display dict. Missing bits degrade to '?'/0
    instead of raising (new accounts / non-comp games have partial data).

    ACS = score / rounds (rounds = red+blue); HS% = head/(head+body+leg);
    result compares my team's round score to the other team's."""
    meta = m.get("meta") or {}
    st = m.get("stats") or {}
    teams = m.get("teams") or {}
    red, blue = teams.get("red", 0), teams.get("blue", 0)
    sh = st.get("shots") or {}
    head = sh.get("head", 0)
    allshots = head + sh.get("body", 0) + sh.get("leg", 0)
    mine, other = (blue, red) if (st.get("team") or "").lower() == "blue" else (red, blue)
    return {
        "map": (meta.get("map") or {}).get("name") or "?",
        "agent": (st.get("character") or {}).get("name") or "?",
        "kda": f"{st.get('kills', 0)}/{st.get('deaths', 0)}/{st.get('assists', 0)}",
        "acs": round(st.get("score", 0) / max(red + blue, 1)),
        "hs": round(100 * head / max(allshots, 1)),
        "result": "W" if mine > other else "L",
        "score": f"{mine}-{other}",
    }


def sparkline(values: list[int]) -> str:
    """Ints -> ▁▂▃▄▅▆▇█ bars, one char per value, scaled across min..max.
    '' for an empty list; a flat series -> all lowest bars (no div-by-zero)."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return BARS[0] * len(values)
    span = hi - lo
    return "".join(BARS[round((v - lo) / span * (len(BARS) - 1))] for v in values)


def _resolve(user_id, name, tag, region):
    """(name, tag, region) from explicit args, else the caller's /link-riot link.
    None if unlinked and no name/tag given. Explicit region overrides the link's."""
    if name and tag:
        return name, tag.lstrip("#"), henrik.valid_region(region)
    link = (rankroles.load().get("links") or {}).get(str(user_id))
    if not link:
        return None
    return link["name"], link["tag"], henrik.valid_region(region) if region else link["region"]


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
    describe = dict(name="Riot name (before #) — omit to use your linked account",
                    tag="Tag (after #)", region="Region (default: your link, else AP)")

    async def _resolve_or_reply(interaction, name, tag, region):
        """Shared front door: no-key / partial-args / unlinked replies. Returns
        (name, tag, region) or None (already responded to the user)."""
        if not henrik.API_KEY:
            await interaction.followup.send(
                "Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.", ephemeral=True)
            return None
        if bool(name) != bool(tag):
            await interaction.followup.send(
                "Give **both** a name and tag, or neither to use your linked account.",
                ephemeral=True)
            return None
        ident = _resolve(interaction.user.id, name, tag, region)
        if not ident:
            await interaction.followup.send(
                "Link your account first with `/link-riot`, or pass a name and tag.",
                ephemeral=True)
            return None
        return ident

    # ---- matches ---------------------------------------------------------
    @bot.tree.command(description="Recent competitive games: map, agent, KDA, ACS, HS%, W/L")
    @app_commands.describe(**describe)
    @app_commands.choices(region=region_choices)
    async def matches(interaction: discord.Interaction, name: str = None,
                      tag: str = None, region: str = None):
        await interaction.response.defer(ephemeral=True)
        ident = await _resolve_or_reply(interaction, name, tag, region)
        if not ident:
            return
        n, t, reg = ident
        status, data = await get(f"/valorant/v1/stored-matches/{reg}/{n}/{t}",
                                 params={"size": "5"})
        if status == 404 or not (data and data.get("data")):
            await interaction.followup.send(
                f"No stored matches for **{n}#{t}** on {reg.upper()}.", ephemeral=True)
            return
        if status != 200:
            await interaction.followup.send("Couldn't reach Henrik. Try again later.", ephemeral=True)
            return
        comp = [m for m in data["data"] if (m.get("meta") or {}).get("mode") == "Competitive"]
        rows = comp or data["data"]  # fall back if they've only played other modes
        e = discord.Embed(title=f"{n}#{t} — last {len(rows)} {'comp ' if comp else ''}games",
                          color=RED)
        for m in rows:
            f = format_match(m)
            e.add_field(name=f"{f['result']}  {f['map']} · {f['agent']}",
                        value=f"{f['kda']} K/D/A · {f['acs']} ACS · {f['hs']}% HS · {f['score']}",
                        inline=False)
        e.set_footer(text=reg.upper())
        await interaction.followup.send(embed=e, ephemeral=True)

    # ---- mmr history -----------------------------------------------------
    @bot.tree.command(name="mmr-history",
                      description="RR over time: sparkline, recent changes, current rank")
    @app_commands.describe(**describe)
    @app_commands.choices(region=region_choices)
    async def mmr_history(interaction: discord.Interaction, name: str = None,
                          tag: str = None, region: str = None):
        await interaction.response.defer(ephemeral=True)
        ident = await _resolve_or_reply(interaction, name, tag, region)
        if not ident:
            return
        n, t, reg = ident
        status, data = await get(f"/valorant/v1/stored-mmr-history/{reg}/{n}/{t}")
        if status == 404 or not (data and data.get("data")):
            await interaction.followup.send(
                f"No RR history for **{n}#{t}** on {reg.upper()}. They may not have played comp.",
                ephemeral=True)
            return
        if status != 200:
            await interaction.followup.send("Couldn't reach Henrik. Try again later.", ephemeral=True)
            return
        hist = data["data"]  # newest-first
        # Sparkline the cumulative sum of real per-game RR deltas, not raw elo:
        # elo/ranking_in_tier jump hundreds at tier boundaries, which would
        # swamp the chart. Summed deltas trace the true RR trajectory.
        deltas = [h.get("last_mmr_change") or 0 for h in reversed(hist)]
        window = deltas[-40:]
        spark = sparkline(list(accumulate(window)))
        net = sum(window)
        cur = hist[0]
        tier = (cur.get("tier") or {}).get("name") or "Unranked"
        rr = cur.get("ranking_in_tier")
        changes = "  ".join(f"{d:+d}" for d in deltas[-8:]) or "—"
        e = discord.Embed(title=f"{n}#{t} — RR history", color=RED)
        e.add_field(name="Current",
                    value=f"**{tier}**" + (f" · {rr} RR" if rr is not None else ""), inline=False)
        e.add_field(name=f"Last {len(window)} games  (net {net:+d} RR)",
                    value=f"```{spark}```", inline=False)
        e.add_field(name="Recent changes", value=changes, inline=False)
        e.set_footer(text=reg.upper())
        await interaction.followup.send(embed=e, ephemeral=True)
