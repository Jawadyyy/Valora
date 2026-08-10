"""Henrik-API commands - rank, crosshair, featured bundle, server status.

Henrik (api.henrikdev.xyz) is a public aggregator keyed by Riot ID or PUUID, so
NONE of this needs a teammate's login cookie - only a free API key in
HENRIK_API_KEY. Much lower risk than the in-game store path.

Dead ends (Riot removed the upstream, Henrik returns 404):
  - store-offers  -> arbitrary skin prices, collection value, savings math.
    Only the CURRENT featured bundle still carries prices (in /featured).

Wire in from the main file:  import henrik; henrik.register(bot)
"""

import os

import aiohttp
import discord
from discord import app_commands

BASE = "https://api.henrikdev.xyz"
RED = 0xFF4655
API_KEY = os.getenv("HENRIK_API_KEY")

REGIONS = ["ap", "na", "eu", "kr", "latam", "br"]
DEFAULT_REGION = "ap"  # Team HEX is AP

# VP has no official real-money rate; this is an editable estimate. VP packs in
# PKR work out to roughly this per point - tune to the current store.
# ponytail: hardcoded rate, move to env if it needs changing often.
VP_TO_PKR = 1.30


# --------------------------------------------------------------------------
# pure helpers - unit tested
# --------------------------------------------------------------------------

def parse_rank(data: dict) -> dict:
    """MMR v3 payload -> flat {tier, rr, peak, peak_season}. Missing bits become
    'Unranked'/None rather than raising, because new accounts have partial data."""
    cur = data.get("current") or {}
    peak = data.get("peak") or {}
    return {
        "tier": (cur.get("tier") or {}).get("name") or "Unranked",
        "rr": cur.get("rr"),
        "elo": cur.get("elo"),
        "peak": (peak.get("tier") or {}).get("name"),
        "peak_season": (peak.get("season") or {}).get("short"),
    }


def vp_to_pkr(vp: int | None) -> str:
    """VP amount -> '~Rs 8,710' estimate. '—' for missing."""
    if not vp:
        return "—"
    return f"~Rs {round(vp * VP_TO_PKR):,}"


def fmt_seconds(seconds: int | None) -> str:
    """807173 -> '9d 8h'. None -> 'unknown'."""
    if not seconds or seconds < 0:
        return "unknown"
    d, rem = divmod(int(seconds), 86400)
    h = rem // 3600
    return f"{d}d {h}h" if d else f"{h}h"


def valid_region(region: str) -> str:
    r = (region or "").lower()
    return r if r in REGIONS else DEFAULT_REGION


async def fetch_mmr(session, region: str, name: str, tag: str) -> dict | None:
    """Shared rank lookup used by /rank and the rank-role system.
    Returns parse_rank(...) or None (bad key / not found / no ranked data)."""
    if not API_KEY:
        return None
    url = f"{BASE}/valorant/v3/mmr/{valid_region(region)}/pc/{name}/{tag.lstrip('#')}"
    try:
        async with session.get(url, headers={"Authorization": API_KEY}) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except aiohttp.ClientError:
        return None
    return parse_rank(data.get("data") or {})


def en(items: list) -> str | None:
    """Pick the English string from a list of {locale, content} translations."""
    for x in items or []:
        if x.get("locale") in ("en_US", "en_GB"):
            return x.get("content")
    return (items or [{}])[0].get("content")


def aggregate_matches(matches: list, competitive_only: bool = True) -> dict | None:
    """Recent stored matches -> {games, kd, kda, hs, acs, wl}. Each match's
    `stats` is already the queried player's. None if there's nothing to sum.

    ACS = total combat score / total rounds; HS% = head / (head+body+leg)."""
    rows = matches
    if competitive_only:
        comp = [m for m in matches if (m.get("meta") or {}).get("mode") == "Competitive"]
        rows = comp or matches  # fall back if they've only played other modes
    k = d = a = head = allshots = score = rounds = wins = games = 0
    for m in rows:
        st = m.get("stats") or {}
        games += 1
        k += st.get("kills", 0); d += st.get("deaths", 0); a += st.get("assists", 0)
        sh = st.get("shots") or {}
        head += sh.get("head", 0)
        allshots += sh.get("head", 0) + sh.get("body", 0) + sh.get("leg", 0)
        score += st.get("score", 0)
        teams = m.get("teams") or {}
        red, blue = teams.get("red", 0), teams.get("blue", 0)
        rounds += red + blue
        mine, other = (blue, red) if (st.get("team") or "").lower() == "blue" else (red, blue)
        if mine > other:
            wins += 1
    if not games:
        return None
    return {
        "games": games,
        "kd": round(k / max(d, 1), 2),
        "kda": f"{k}/{d}/{a}",
        "hs": round(100 * head / max(allshots, 1)),
        "acs": round(score / max(rounds, 1)),
        "wl": f"{wins}-{games - wins}",
    }


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def register(bot):
    async def get(path, **kw):
        """Henrik GET -> (status, json|bytes). Returns (0, None) if no key set."""
        if not API_KEY:
            return 0, None
        async with bot.session.get(f"{BASE}{path}", headers={"Authorization": API_KEY}, **kw) as r:
            if "image" in r.content_type:
                return r.status, await r.read()
            return r.status, await r.json()

    region_choices = [app_commands.Choice(name=r.upper(), value=r) for r in REGIONS]

    # ---- rank ------------------------------------------------------------
    @bot.tree.command(description="Look up someone's rank, RR and peak")
    @app_commands.describe(name="Riot name (before the #)", tag="Tag (after the #)",
                           region="Region (default AP)")
    @app_commands.choices(region=region_choices)
    async def rank(interaction: discord.Interaction, name: str, tag: str,
                   region: str = DEFAULT_REGION):
        await interaction.response.defer()
        if not API_KEY:
            await interaction.followup.send("Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.")
            return
        reg = valid_region(region)
        tag = tag.lstrip("#")
        status, data = await get(f"/valorant/v3/mmr/{reg}/pc/{name}/{tag}")
        if status == 404:
            await interaction.followup.send(
                f"No ranked data for **{name}#{tag}** on {reg.upper()}. "
                "Either the name/tag/region is wrong, or they haven't played comp.")
            return
        if status != 200 or not data:
            await interaction.followup.send("Couldn't reach Henrik. Try again later.")
            return
        r = parse_rank(data["data"])
        e = discord.Embed(title=f"{name}#{tag}", color=RED)
        e.add_field(name="Rank", value=r["tier"] + (f"  ·  {r['rr']} RR" if r["rr"] is not None else ""))
        if r["peak"]:
            e.add_field(name="Peak", value=r["peak"] + (f"  ({r['peak_season']})" if r["peak_season"] else ""))

        # Recent-form stats from stored match history. Best-effort - rank still
        # shows if this fails or the player has no stored games.
        mstatus, mdata = await get(f"/valorant/v1/stored-matches/{reg}/{name}/{tag}",
                                   params={"size": "10"})
        stats = aggregate_matches(mdata["data"]) if mstatus == 200 and mdata and mdata.get("data") else None
        if stats:
            e.add_field(name="K/D", value=f"{stats['kd']}  ({stats['kda']})")
            e.add_field(name="Headshot %", value=f"{stats['hs']}%")
            e.add_field(name="ACS", value=str(stats["acs"]))
            e.add_field(name=f"Last {stats['games']} comp", value=f"{stats['wl']}  W-L")
        e.set_footer(text=reg.upper())
        await interaction.followup.send(embed=e)

    # ---- crosshair -------------------------------------------------------
    @bot.tree.command(description="Render a crosshair from its share code")
    @app_commands.describe(code="Crosshair share code (copied in-game)")
    async def crosshair(interaction: discord.Interaction, code: str):
        await interaction.response.defer()
        if not API_KEY:
            await interaction.followup.send("Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.")
            return
        status, img = await get("/valorant/v1/crosshair/generate", params={"id": code})
        if status != 200 or not isinstance(img, (bytes, bytearray)):
            await interaction.followup.send(
                "That code didn't render. Copy the share code from Valorant "
                "(Settings → Crosshair → Copy code) and paste it exactly.")
            return
        import io
        f = discord.File(io.BytesIO(img), filename="crosshair.png")
        e = discord.Embed(title="Crosshair", color=RED).set_image(url="attachment://crosshair.png")
        e.set_footer(text=code[:120])
        await interaction.followup.send(embed=e, file=f)

    # ---- featured bundle -------------------------------------------------
    @bot.tree.command(description="Current featured bundle + PKR price")
    async def featured(interaction: discord.Interaction):
        await interaction.response.defer()
        if not API_KEY:
            await interaction.followup.send("Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.")
            return
        status, data = await get("/valorant/v2/store-featured")
        if status != 200 or not data or not data.get("data"):
            await interaction.followup.send("Couldn't fetch the featured bundle. Try again later.")
            return
        embeds = []
        for b in data["data"][:3]:
            price = b.get("bundle_price")
            name = await _bundle_name(bot.session, b.get("bundle_uuid"))
            e = discord.Embed(
                title=name,
                description=f"**{price} VP**  ·  {vp_to_pkr(price)}\n"
                            f"Ends in **{fmt_seconds(b.get('seconds_remaining'))}**",
                color=RED)
            items = b.get("items") or []
            listing = "\n".join(
                f"• {i.get('name')} — {i.get('discounted_price') or i.get('base_price')} VP"
                for i in items[:12] if i.get("name"))
            if listing:
                e.add_field(name=f"{len(items)} items", value=listing[:1024], inline=False)
            embeds.append(e)
        await interaction.followup.send(embeds=embeds)

    # ---- server status ---------------------------------------------------
    @bot.tree.command(description="Is Valorant down? (per region)")
    @app_commands.choices(region=region_choices)
    async def status(interaction: discord.Interaction, region: str = DEFAULT_REGION):
        await interaction.response.defer()
        if not API_KEY:
            await interaction.followup.send("Henrik API key not set. Add `HENRIK_API_KEY` to `.env`.")
            return
        reg = valid_region(region)
        st, data = await get(f"/valorant/v1/status/{reg}")
        if st != 200 or not data:
            await interaction.followup.send("Couldn't reach Riot status. Try again later.")
            return
        d = data.get("data") or {}
        incidents = d.get("incidents") or []
        maints = d.get("maintenances") or []
        if not incidents and not maints:
            await interaction.followup.send(f"✅ Valorant **{reg.upper()}** looks healthy — no incidents or maintenance.")
            return

        sev_icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🔴"}
        e = discord.Embed(title=f"Valorant {reg.upper()} — active reports", color=RED)
        for x in (incidents + maints)[:8]:
            title = en(x.get("titles")) or "Report"
            ups = x.get("updates") or []
            detail = en(ups[0].get("translations") or []) if ups else None
            icon = sev_icon.get(x.get("incident_severity"), "•")
            plats = ", ".join(x.get("platforms") or [])
            body = (detail or "No details.")[:300]
            if plats:
                body += f"\n*{plats}*"
            e.add_field(name=f"{icon} {title}", value=body, inline=False)
        await interaction.followup.send(embed=e)


async def _bundle_name(session: aiohttp.ClientSession, uuid: str | None) -> str:
    """Resolve a bundle uuid to its display name via valorant-api. Falls back to
    a generic label so /featured never fails on a missing name."""
    if not uuid:
        return "Featured bundle"
    try:
        async with session.get(f"https://valorant-api.com/v1/bundles/{uuid}") as r:
            data = (await r.json()).get("data") or {}
        return data.get("displayName") or "Featured bundle"
    except aiohttp.ClientError:
        return "Featured bundle"
