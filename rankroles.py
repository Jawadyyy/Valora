"""Auto rank roles - link a Riot ID, get a Discord role matching your rank,
re-checked weekly. Uses Henrik (riot-ID keyed, no cookie).

Storage (rankroles.json):
  {"links":  {discord_id: {name, tag, region}},          # who you are on Riot
   "guilds": {guild_id:   {base_tier: role_id}}}          # this server's tier roles

The Riot link here is also reused by /matches, MMR history, and the balancer.

Requires the bot to have the **Manage Roles** permission, and its own role must
sit ABOVE the rank roles in the server list (auto-created roles start below it,
so this holds unless someone reorders them).
"""

import asyncio
import json
import os
import pathlib

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

import henrik

RED = 0xFF4655
STORE = pathlib.Path(__file__).with_name("rankroles.json")

# base tiers (no numbers) + a Discord role colour each, low -> high
TIERS = [
    ("Iron", 0x4d4d4d), ("Bronze", 0xa1683a), ("Silver", 0xc0c0c0),
    ("Gold", 0xf1c40f), ("Platinum", 0x1abc9c), ("Diamond", 0xb57edc),
    ("Ascendant", 0x1d7a4d), ("Immortal", 0xa4243b), ("Radiant", 0xffe082),
]
TIER_NAMES = [t for t, _ in TIERS]
_bot = None


# --------------------------------------------------------------------------
# storage + pure helpers
# --------------------------------------------------------------------------

def load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(d: dict) -> None:
    tmp = STORE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STORE)


def base_tier(rank_name: str | None) -> str | None:
    """'Platinum 2' -> 'Platinum', 'Radiant' -> 'Radiant', 'Unranked'/None -> None."""
    if not rank_name:
        return None
    first = rank_name.split()[0]
    return first if first in TIER_NAMES else None


# --------------------------------------------------------------------------
# role assignment
# --------------------------------------------------------------------------

async def apply_role(member: discord.Member, guild_cfg: dict, tier: str | None):
    """Give the member their tier role, remove any other managed tier roles.
    Silently no-ops on permission/hierarchy errors (logged by the caller)."""
    managed_ids = {int(v) for v in guild_cfg.values()}
    target_id = int(guild_cfg[tier]) if tier and tier in guild_cfg else None

    remove = [r for r in member.roles if r.id in managed_ids and r.id != target_id]
    if remove:
        await member.remove_roles(*remove, reason="rank role update")
    if target_id:
        role = member.guild.get_role(target_id)
        if role and role not in member.roles:
            await member.add_roles(role, reason="rank role update")


async def refresh_member(member: discord.Member, ident: dict, guild_cfg: dict) -> str | None:
    """Look up the member's rank and set their role. Returns the tier applied,
    or None if unranked / lookup failed."""
    rank = await henrik.fetch_mmr(_bot.session, ident["region"], ident["name"], ident["tag"])
    tier = base_tier(rank["tier"]) if rank else None
    await apply_role(member, guild_cfg, tier)
    return tier


# --------------------------------------------------------------------------
# weekly recheck
# --------------------------------------------------------------------------

@tasks.loop(hours=24 * 7)
async def recheck_job():
    data = load()
    links, guilds = data.get("links") or {}, data.get("guilds") or {}
    for gid, cfg in guilds.items():
        guild = _bot.get_guild(int(gid))
        if not guild or not cfg:
            continue
        for uid, ident in links.items():
            try:
                member = await guild.fetch_member(int(uid))
            except (discord.NotFound, discord.HTTPException):
                continue  # not in this server
            try:
                await refresh_member(member, ident, cfg)
            except discord.Forbidden:
                pass  # missing Manage Roles / hierarchy - skip, don't crash
            await asyncio.sleep(1)  # be gentle on Henrik's rate limit


@recheck_job.before_loop
async def _wait():
    await _bot.wait_until_ready()


def start_jobs():
    if not recheck_job.is_running():
        recheck_job.start()


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def register(bot):
    global _bot
    _bot = bot

    @bot.tree.command(name="link-riot", description="Link your Riot ID for rank roles & stats")
    @app_commands.describe(name="Riot name (before #)", tag="Tag (after #)",
                           region="Region (default AP)")
    @app_commands.choices(region=[app_commands.Choice(name=r.upper(), value=r) for r in henrik.REGIONS])
    async def link_riot(interaction: discord.Interaction, name: str, tag: str, region: str = "ap"):
        await interaction.response.defer(ephemeral=True)
        rank = await henrik.fetch_mmr(bot.session, region, name, tag)
        if rank is None:
            await interaction.followup.send(
                f"Couldn't find ranked data for **{name}#{tag.lstrip('#')}** on {region.upper()}. "
                "Check the name/tag/region — or they may not have played comp.", ephemeral=True)
            return
        data = load()
        data.setdefault("links", {})[str(interaction.user.id)] = {
            "name": name, "tag": tag.lstrip("#"), "region": henrik.valid_region(region)}
        save(data)

        msg = f"Linked **{name}#{tag.lstrip('#')}** — rank **{rank['tier']}**."
        cfg = (data.get("guilds") or {}).get(str(interaction.guild_id))
        if cfg and isinstance(interaction.user, discord.Member):
            try:
                tier = await refresh_member(interaction.user, data["links"][str(interaction.user.id)], cfg)
                if tier:
                    msg += f"\nGave you the **{tier}** role."
            except discord.Forbidden:
                msg += "\n(Couldn't assign your role — the bot needs Manage Roles above the rank roles.)"
        await interaction.followup.send(msg, ephemeral=True)

    @bot.tree.command(name="rank-role", description="Refresh your rank role now")
    async def rank_role(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = load()
        ident = (data.get("links") or {}).get(str(interaction.user.id))
        if not ident:
            await interaction.followup.send("Link your account first with `/link-riot`.", ephemeral=True)
            return
        cfg = (data.get("guilds") or {}).get(str(interaction.guild_id))
        if not cfg:
            await interaction.followup.send(
                "Rank roles aren't set up on this server yet. An admin runs `/setup-rank-roles`.", ephemeral=True)
            return
        try:
            tier = await refresh_member(interaction.user, ident, cfg)
        except discord.Forbidden:
            await interaction.followup.send(
                "I couldn't change your roles — I need **Manage Roles**, and my role must sit above the rank roles.",
                ephemeral=True)
            return
        await interaction.followup.send(
            f"Updated — you're **{tier}**." if tier else "You're currently Unranked, so no role was assigned.",
            ephemeral=True)

    @bot.tree.command(name="setup-rank-roles", description="Admin: create the rank roles on this server")
    @app_commands.default_permissions(manage_roles=True)
    async def setup_rank_roles(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return
        if not interaction.guild.me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I don't have the **Manage Roles** permission. Give it to me and re-run.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        existing = {r.name: r for r in interaction.guild.roles}
        mapping, created = {}, 0
        for tname, colour in TIERS:
            role = existing.get(tname)
            if role is None:
                try:
                    role = await interaction.guild.create_role(
                        name=tname, colour=discord.Colour(colour), reason="rank roles setup")
                    created += 1
                except discord.Forbidden:
                    await interaction.followup.send("Failed creating roles — check my permissions.", ephemeral=True)
                    return
            mapping[tname] = str(role.id)
        data = load()
        data.setdefault("guilds", {})[str(interaction.guild_id)] = mapping
        save(data)
        await interaction.followup.send(
            f"Rank roles ready ({created} created, {len(mapping)-created} reused). "
            "Members link with `/link-riot` to get their role; I re-check weekly.\n"
            "-# Make sure my role is dragged **above** these tier roles in Server Settings → Roles.",
            ephemeral=True)
