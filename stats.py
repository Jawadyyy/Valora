"""Per-game history (/matches) and RR-over-time (/mmr-history).

Both default to the caller's /link-riot identity (stored in rankroles.json) and
accept a name/tag/region override. Henrik-keyed, no cookie - see henrik.py.

Wire in from the main file:  import stats; stats.register(bot)
"""

import time
from itertools import accumulate

import discord
from discord import app_commands

import henrik
import rankroles

RED = henrik.RED
BARS = "▁▂▃▄▅▆▇█"

# competitive tier groups (each spans 3 sub-ranks); id 27 = Radiant, <3 = Unranked
_TIER_GROUPS = ["Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond",
                "Ascendant", "Immortal"]

# regional leaderboard is shared + rarely changes -> cache per region, 5 min
_LB_TTL = 300
_lb_cache: dict = {}  # region -> {"at": ts, "data": [players]}


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


def tier_name(tier_id) -> str:
    """Competitive tier id -> name. 27 Radiant, 24-26 Immortal 1-3, 3-5 Iron 1-3,
    anything below 3 (or None) Unranked. The leaderboard gives ids, not names."""
    if not tier_id or tier_id < 3:
        return "Unranked"
    if tier_id >= 27:
        return "Radiant"
    return f"{_TIER_GROUPS[(tier_id - 3) // 3]} {(tier_id - 3) % 3 + 1}"


def leaderboard_rows(players: list, tier: str | None = None, top: int = 15) -> list[dict]:
    """Leaderboard players -> [{rank, name, tag, rr, tier}], sorted by rank,
    optionally filtered to a base tier ('Immortal' keeps Immortal 1/2/3), capped
    to `top`. Anonymized/blank names become '(anonymous)'."""
    rows = []
    for p in sorted(players, key=lambda x: x.get("leaderboard_rank") or 0):
        tname = tier_name(p.get("tier"))
        if tier and tname.split()[0].lower() != tier.lower():
            continue
        name = "(anonymous)" if p.get("is_anonymized") or not p.get("name") else p["name"]
        rows.append({"rank": p.get("leaderboard_rank"), "name": name,
                     "tag": p.get("tag") or "", "rr": p.get("rr"), "tier": tname})
        if len(rows) >= top:
            break
    return rows


def balance_teams(players: list) -> tuple[dict, dict]:
    """(name, elo) pairs -> two rank-balanced teams by snake-drafting elo desc
    (1st->A, 2nd,3rd->B, 4th,5th->A, …), which keeps sizes within 1 and totals
    close. Each team: {players:[(name,elo)], total, avg}. Handles odd/empty.

    ponytail: snake draft, not an exhaustive min-gap partition. For <=10 players
    a brute-force C(n,n/2) search would shave the last few elo off the gap; add
    it only if perfectly minimal gaps ever matter."""
    ranked = sorted(players, key=lambda p: p[1], reverse=True)
    picks = ([], [])
    for i, p in enumerate(ranked):
        picks[0 if i % 4 in (0, 3) else 1].append(p)

    def pack(team):
        total = sum(e for _, e in team)
        return {"players": team, "total": total,
                "avg": round(total / len(team)) if team else 0}
    return pack(picks[0]), pack(picks[1])


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

    # ---- leaderboard -----------------------------------------------------
    tier_choices = [app_commands.Choice(name=t, value=t) for t in
                    ("Radiant", "Immortal", "Ascendant", "Diamond", "Platinum",
                     "Gold", "Silver", "Bronze", "Iron")]

    async def _fetch_leaderboard(reg):
        """Cached (5 min) leaderboard players for a region. None on failure with
        no cached copy; a stale copy if the refresh fails."""
        c = _lb_cache.get(reg)
        if c and time.time() - c["at"] < _LB_TTL:
            return c["data"]
        status, data = await get(f"/valorant/v3/leaderboard/{reg}/pc")
        if status != 200 or not data:
            return c["data"] if c else None
        players = (data.get("data") or {}).get("players") or []
        _lb_cache[reg] = {"at": time.time(), "data": players}
        return players

    @bot.tree.command(description="Regional ranked leaderboard — top players")
    @app_commands.describe(region="Region (default AP)", tier="Filter to a tier")
    @app_commands.choices(region=region_choices, tier=tier_choices)
    async def leaderboard(interaction: discord.Interaction,
                          region: str = henrik.DEFAULT_REGION, tier: str = None):
        await interaction.response.defer()
        if not henrik.API_KEY:
            await interaction.followup.send("Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.")
            return
        reg = henrik.valid_region(region)
        players = await _fetch_leaderboard(reg)
        if players is None:
            await interaction.followup.send("Couldn't fetch the leaderboard. Try again later.")
            return
        rows = leaderboard_rows(players, tier=tier, top=15)
        if not rows:
            await interaction.followup.send(
                f"No **{tier}** players on the {reg.upper()} leaderboard." if tier
                else f"The {reg.upper()} leaderboard is empty right now.")
            return
        lines = [f"`#{r['rank']:>3}` **{r['name']}**#{r['tag']} · {r['rr']} RR · {r['tier']}"
                 for r in rows]
        e = discord.Embed(title=f"{reg.upper()} Leaderboard" + (f" — {tier}" if tier else ""),
                          description="\n".join(lines), color=RED)
        e.set_footer(text="Top ranked · cached 5 min")
        await interaction.followup.send(embed=e)

    # ---- balance ---------------------------------------------------------
    @bot.tree.command(description="Split your voice channel into two rank-balanced teams")
    async def balance(interaction: discord.Interaction):
        await interaction.response.defer()
        if not henrik.API_KEY:
            await interaction.followup.send("Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.")
            return
        voice = getattr(interaction.user, "voice", None)
        if not voice or not voice.channel:
            await interaction.followup.send(
                "Join a voice channel first — I balance whoever's in it.", ephemeral=True)
            return
        links = rankroles.load().get("links") or {}
        players, skipped = [], []
        for m in voice.channel.members:
            if m.bot:
                continue
            ident = links.get(str(m.id))
            if not ident:
                skipped.append(m.display_name)
                continue
            rank = await henrik.fetch_mmr(bot.session, ident["region"], ident["name"], ident["tag"])
            elo = rank.get("elo") if rank else None
            if not elo:
                skipped.append(f"{m.display_name} (unranked)")
                continue
            players.append((m.display_name, elo))
        if len(players) < 2:
            await interaction.followup.send(
                "Need at least 2 linked, ranked players in the channel — others run `/link-riot`."
                + (f"\nSkipped: {', '.join(skipped)}" if skipped else ""))
            return
        a, b = balance_teams(players)
        # avg-elo gap, not total: with an odd count the teams differ by a player,
        # so the total-elo gap is dominated by size, not by strength.
        gap = abs(a["avg"] - b["avg"])
        fmt = lambda team: "\n".join(f"• {n} — {e}" for n, e in team["players"]) or "—"
        e = discord.Embed(title="Balanced teams", color=RED,
                          description=f"Avg elo gap: **{gap}**")
        e.add_field(name=f"Team A · avg {a['avg']} elo", value=fmt(a), inline=True)
        e.add_field(name=f"Team B · avg {b['avg']} elo", value=fmt(b), inline=True)
        if skipped:
            e.add_field(name=f"Skipped ({len(skipped)})",
                        value=", ".join(skipped)[:1024], inline=False)
        await interaction.followup.send(embed=e)
