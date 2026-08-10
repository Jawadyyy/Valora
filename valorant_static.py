"""Static-data commands - skins, agents, weapons, maps, cosmetics, randoms.

Everything here comes from valorant-api.com: public, no key, no login, no risk.
Kept separate from valorant_bot_simple.py because it shares nothing with the
cookie/auth/store code.

NOT here (need a source valorant-api doesn't have):
  - prices / collection value / bundle math  -> need Henrik's public store-offers
    (free API key, no user login). Separate batch.
  - ability costs / cooldowns  -> not in any free dataset. /agent shows what
    valorant-api actually has (slot, name, description).

Wire it in from the main file:  import valorant_static; valorant_static.register(bot)
"""

import json
import os
import pathlib
import random

import aiohttp
import discord
from discord import app_commands

VAPI = "https://valorant-api.com/v1"
RED = 0xFF4655

SCORES_FILE = pathlib.Path(__file__).with_name("trivia_scores.json")
TRIVIA_TOPICS = ["skin", "agent", "weapon"]

# One fetch per catalogue per process. valorant-api is static within a patch.
_cache: dict[str, list] = {}
_tiers: dict[str, dict] = {}


async def catalogue(session: aiohttp.ClientSession, path: str) -> list:
    """Fetch + cache a valorant-api list endpoint. [] on failure so a dead
    lookup degrades to 'not found' instead of crashing the command."""
    if path not in _cache:
        try:
            async with session.get(f"{VAPI}/{path}") as r:
                _cache[path] = (await r.json()).get("data") or []
        except aiohttp.ClientError:
            return []
    return _cache[path]


async def tiers(session: aiohttp.ClientSession) -> dict[str, dict]:
    """Content-tier uuid -> {displayName, highlightColor}. Cached."""
    if not _tiers:
        for t in await catalogue(session, "contenttiers"):
            _tiers[t["uuid"]] = t
    return _tiers


# --------------------------------------------------------------------------
# pure helpers - unit tested
# --------------------------------------------------------------------------

def best_match(query: str, items: list, key="displayName") -> dict | None:
    """Case-insensitive lookup. Exact wins; else the shortest name that contains
    the query (so 'vandal' prefers 'Vandal' over 'Reaver Vandal')."""
    q = query.strip().lower()
    if not q:
        return None
    named = [(i.get(key) or "", i) for i in items]
    for name, i in named:
        if name.lower() == q:
            return i
    subs = [(name, i) for name, i in named if q in name.lower()]
    if not subs:
        return None
    return min(subs, key=lambda pair: len(pair[0]))[1]


def tier_color(tier: dict | None) -> int:
    """highlightColor is 8-hex RRGGBBAA; take RGB. Fall back to Valorant red."""
    hc = (tier or {}).get("highlightColor") or ""
    try:
        return int(hc[:6], 16)
    except ValueError:
        return RED


def fmt_damage(ranges: list) -> str:
    """damageRanges -> 'm0-15: 160/40/34' lines (head/body/leg)."""
    out = []
    for r in ranges:
        a, b = int(r.get("rangeStartMeters", 0)), int(r.get("rangeEndMeters", 0))
        out.append(f"`{a}-{b}m`  {round(r.get('headDamage',0))} / "
                   f"{round(r.get('bodyDamage',0))} / {round(r.get('legDamage',0))}")
    return "\n".join(out) or "—"


def pick_loadout(weapons: list, wanted: list[str]) -> list[tuple[str, str]]:
    """For each wanted weapon name, pick a random skin. Returns [(weapon, skin)].
    Skips a weapon that has no skins or isn't found - never crashes on a random."""
    out = []
    by_name = {w["displayName"].lower(): w for w in weapons}
    for name in wanted:
        w = by_name.get(name.lower())
        skins = [s for s in (w or {}).get("skins", []) if s.get("displayName")]
        if skins:
            # drop the boring "Standard <gun>" default from the pool if others exist
            fancy = [s for s in skins if "Standard" not in s["displayName"]] or skins
            out.append((name, random.choice(fancy)["displayName"]))
    return out


LOADOUT_WEAPONS = ["Classic", "Sheriff", "Spectre", "Vandal", "Phantom", "Operator", "Melee"]


def make_options(items: list, correct: str, key="displayName", n=4) -> list[str]:
    """Correct answer + (n-1) random distractors, deduped and shuffled.
    Fewer than n if the pool is small - never raises."""
    pool = list({(i.get(key) or "") for i in items} - {correct, ""})
    distractors = random.sample(pool, min(n - 1, len(pool)))
    opts = distractors + [correct]
    random.shuffle(opts)
    return opts


# --- trivia scoring (file-backed) -----------------------------------------

def load_scores() -> dict:
    try:
        with open(SCORES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_scores(scores: dict) -> None:
    tmp = SCORES_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(scores, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SCORES_FILE)


def apply_result(scores: dict, uid: str, name: str, correct: bool) -> dict:
    """Record one answer. Mutates + returns that user's entry.
    Streak resets on a miss; best_streak is the high-water mark."""
    e = scores.setdefault(uid, {"name": name, "correct": 0, "attempts": 0,
                                "streak": 0, "best_streak": 0})
    e["name"] = name
    e["attempts"] += 1
    if correct:
        e["correct"] += 1
        e["streak"] += 1
        e["best_streak"] = max(e["best_streak"], e["streak"])
    else:
        e["streak"] = 0
    return e


def leaderboard_rows(scores: dict, top: int = 10) -> list[dict]:
    """Top players by points (correct answers), with accuracy%. Ties broken by
    best streak then accuracy."""
    rows = []
    for e in scores.values():
        attempts = e.get("attempts", 0)
        rows.append({
            "name": e.get("name", "?"),
            "correct": e.get("correct", 0),
            "best_streak": e.get("best_streak", 0),
            "accuracy": round(100 * e.get("correct", 0) / attempts) if attempts else 0,
        })
    rows.sort(key=lambda r: (r["correct"], r["best_streak"], r["accuracy"]), reverse=True)
    return rows[:top]


# --------------------------------------------------------------------------
# command registration
# --------------------------------------------------------------------------

def register(bot):
    S = lambda: bot.session  # session is live by the time any command runs

    def _ac(path):
        async def auto(interaction: discord.Interaction, current: str):
            items = await catalogue(S(), path)
            q = current.lower()
            hits = [i for i in items if q in (i.get("displayName") or "").lower()][:25]
            return [app_commands.Choice(name=i["displayName"][:100],
                                        value=i["displayName"][:100]) for i in hits]
        return auto

    async def _lookup(interaction, path, name, build):
        await interaction.response.defer()
        item = best_match(name, await catalogue(S(), path))
        if not item:
            await interaction.followup.send(f"No match for **{name}**.")
            return
        await interaction.followup.send(embed=await build(item))

    # ---- skins -----------------------------------------------------------
    @bot.tree.command(description="Look up a weapon skin")
    @app_commands.describe(name="Skin name, e.g. Reaver Vandal")
    @app_commands.autocomplete(name=_ac("weapons/skins"))
    async def skin(interaction: discord.Interaction, name: str):
        async def build(sk):
            tier = (await tiers(S())).get(sk.get("contentTierUuid"))
            e = discord.Embed(title=sk["displayName"], color=tier_color(tier))
            icon = sk.get("displayIcon") or (sk["levels"][-1] or {}).get("displayIcon")
            if icon:
                e.set_image(url=icon)
            if tier:
                e.add_field(name="Tier", value=tier["displayName"])
            e.add_field(name="Levels", value=str(len(sk.get("levels") or [])))
            chromas = [c for c in sk.get("chromas") or [] if c.get("displayIcon")]
            e.add_field(name="Chromas", value=str(len(chromas)))
            return e
        await _lookup(interaction, "weapons/skins", name, build)

    # ---- agents ----------------------------------------------------------
    @bot.tree.command(description="Agent + ability reference")
    @app_commands.describe(name="Agent name, e.g. Jett")
    @app_commands.autocomplete(name=_ac("agents"))
    async def agent(interaction: discord.Interaction, name: str):
        async def build(a):
            role = (a.get("role") or {}).get("displayName", "—")
            e = discord.Embed(title=f"{a['displayName']}  ·  {role}",
                              description=a.get("description", "")[:400], color=RED)
            if a.get("displayIcon"):
                e.set_thumbnail(url=a["displayIcon"])
            for ab in a.get("abilities") or []:
                if not ab.get("displayName"):
                    continue
                slot = (ab.get("slot") or "").replace("Ability", "Q/E/C").split("::")[-1]
                desc = (ab.get("description") or "—")[:180]
                e.add_field(name=f"{ab['displayName']} ({slot})", value=desc, inline=False)
            e.set_footer(text="valorant-api has no ability cost/cooldown data")
            return e
        # agents endpoint includes non-playable dupes; filter
        await interaction.response.defer()
        agents = [x for x in await catalogue(S(), "agents") if x.get("isPlayableCharacter")]
        a = best_match(name, agents)
        if not a:
            await interaction.followup.send(f"No match for **{name}**.")
            return
        await interaction.followup.send(embed=await build(a))

    # ---- weapons ---------------------------------------------------------
    @bot.tree.command(description="Weapon stats (damage, fire rate, cost)")
    @app_commands.describe(name="Weapon name, e.g. Vandal")
    @app_commands.autocomplete(name=_ac("weapons"))
    async def weapon(interaction: discord.Interaction, name: str):
        async def build(w):
            st = w.get("weaponStats") or {}
            shop = w.get("shopData") or {}
            e = discord.Embed(title=w["displayName"],
                              description=shop.get("categoryText", w.get("category", "")),
                              color=RED)
            if w.get("displayIcon"):
                e.set_thumbnail(url=w["displayIcon"])
            if shop.get("cost") is not None:
                e.add_field(name="Cost", value=f"{shop['cost']} creds")
            if st.get("fireRate"):
                e.add_field(name="Fire rate", value=f"{st['fireRate']}/s")
            if st.get("magazineSize"):
                e.add_field(name="Magazine", value=str(st["magazineSize"]))
            if st.get("wallPenetration"):
                e.add_field(name="Wall pen",
                            value=st["wallPenetration"].split("::")[-1])
            if st.get("damageRanges"):
                e.add_field(name="Damage  (head / body / leg)",
                            value=fmt_damage(st["damageRanges"]), inline=False)
            return e
        await _lookup(interaction, "weapons", name, build)

    # ---- maps ------------------------------------------------------------
    @bot.tree.command(description="Map layout (top-down) + callouts")
    @app_commands.describe(name="Map name, e.g. Ascent")
    @app_commands.autocomplete(name=_ac("maps"))
    async def map(interaction: discord.Interaction, name: str):
        async def build(m):
            e = discord.Embed(title=m["displayName"], color=RED)
            # displayIcon is the top-down tactical layout; splash is loading art.
            layout = m.get("displayIcon") or m.get("splash")
            if layout:
                e.set_image(url=layout)
            calls = sorted({c.get("regionName", "") for c in (m.get("callouts") or [])
                            if c.get("regionName")})
            if calls:
                e.add_field(name=f"Callouts ({len(calls)})",
                            value=", ".join(calls)[:1024], inline=False)
            return e
        await _lookup(interaction, "maps", name, build)

    # ---- simple cosmetics ------------------------------------------------
    def cosmetic_cmd(cmd_name, path, desc):
        @bot.tree.command(name=cmd_name, description=desc)
        @app_commands.autocomplete(name=_ac(path))
        async def _cmd(interaction: discord.Interaction, name: str):
            async def build(it):
                e = discord.Embed(title=it.get("displayName") or it.get("titleText") or "—",
                                  color=RED)
                icon = it.get("displayIcon") or it.get("fullIcon")
                if icon:
                    e.set_thumbnail(url=icon)
                return e
            await _lookup(interaction, path, name, build)
        return _cmd

    cosmetic_cmd("buddy", "buddies", "Gun buddy lookup")
    cosmetic_cmd("spray", "sprays", "Spray lookup")
    cosmetic_cmd("card", "playercards", "Player card lookup")
    cosmetic_cmd("title", "playertitles", "Player title lookup")

    # ---- randoms ---------------------------------------------------------
    @bot.tree.command(name="random-agent", description="Pick a random agent")
    async def random_agent(interaction: discord.Interaction):
        await interaction.response.defer()
        agents = [x for x in await catalogue(S(), "agents") if x.get("isPlayableCharacter")]
        if not agents:
            await interaction.followup.send("Agent list unavailable. Try again later.")
            return
        a = random.choice(agents)
        e = discord.Embed(title=f"🎲 {a['displayName']}",
                          description=(a.get("role") or {}).get("displayName", ""), color=RED)
        if a.get("fullPortrait") or a.get("displayIcon"):
            e.set_image(url=a.get("fullPortrait") or a["displayIcon"])
        await interaction.followup.send(embed=e)

    @bot.tree.command(name="random-loadout", description="Random skins for a full buy")
    async def random_loadout(interaction: discord.Interaction):
        await interaction.response.defer()
        weapons = await catalogue(S(), "weapons")
        picks = pick_loadout(weapons, LOADOUT_WEAPONS)
        if not picks:
            await interaction.followup.send("Weapon data unavailable. Try again later.")
            return
        body = "\n".join(f"**{gun}** — {skin}" for gun, skin in picks)
        await interaction.followup.send(
            embed=discord.Embed(title="🎲 Random loadout", description=body, color=RED))

    # ---- trivia ----------------------------------------------------------
    @bot.tree.command(description="Endless trivia — a new question after every answer")
    @app_commands.describe(topic="Pick a topic, or leave blank for a random mix")
    @app_commands.choices(topic=[
        app_commands.Choice(name="Mixed (random each time)", value="mixed"),
        app_commands.Choice(name="Skin (name the skin)", value="skin"),
        app_commands.Choice(name="Agent (name from ability)", value="agent"),
        app_commands.Choice(name="Weapon (name from stats)", value="weapon"),
    ])
    async def trivia(interaction: discord.Interaction, topic: str = "mixed"):
        await interaction.response.defer()
        built = await new_question(S(), None if topic == "mixed" else topic)
        if not built:
            await interaction.followup.send("Couldn't build a question. Try again.")
            return
        embed, view = built
        await interaction.followup.send(embed=embed, view=view)

    @bot.tree.command(name="trivia-leaderboard", description="Top trivia players on the server")
    async def trivia_leaderboard(interaction: discord.Interaction):
        rows = leaderboard_rows(load_scores())
        if not rows:
            await interaction.response.send_message("No games played yet. Start with `/trivia`!")
            return
        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        lines = [f"{medals[i]} **{r['name']}** — {r['correct']} pts"
                 f"  ·  {r['best_streak']}🔥 best  ·  {r['accuracy']}% acc"
                 for i, r in enumerate(rows)]
        e = discord.Embed(title="🏆 Trivia Leaderboard", description="\n".join(lines), color=RED)
        await interaction.response.send_message(embed=e)


async def _topic_items(session, topic):
    if topic == "agent":
        return [a for a in await catalogue(session, "agents") if a.get("isPlayableCharacter")]
    return await catalogue(session, "weapons/skins" if topic == "skin" else "weapons")


async def _build_question(session, topic):
    """Return (answer_name, prompt_text, image_url_or_None) for the topic."""
    items = await _topic_items(session, topic)
    if not items:
        return None
    if topic == "skin":
        pick = random.choice([s for s in items if s.get("displayIcon")] or items)
        return pick["displayName"], "**Name this skin:**", pick.get("displayIcon")
    if topic == "agent":
        pick = random.choice(items)
        abilities = [a for a in pick.get("abilities") or [] if a.get("description")]
        if not abilities:
            return None
        ab = random.choice(abilities)
        # strip the ability's own name so it doesn't give the agent away
        clue = ab["description"].replace(pick["displayName"], "this agent")
        return pick["displayName"], f"**Which agent has this ability?**\n\n*{clue[:400]}*", None
    # weapon
    pick = random.choice([w for w in items if w.get("weaponStats")] or items)
    st = pick.get("weaponStats") or {}
    shop = pick.get("shopData") or {}
    clue = (f"Cost **{shop.get('cost','?')}** · fire rate **{st.get('fireRate','?')}/s** · "
            f"magazine **{st.get('magazineSize','?')}**")
    return pick["displayName"], f"**Name this weapon:**\n{clue}", None


async def new_question(session, topic=None):
    """Build (embed, TriviaView) for a topic, or a random one if topic is None.
    None if the data isn't available."""
    topic = topic or random.choice(TRIVIA_TOPICS)
    q = await _build_question(session, topic)
    if not q:
        return None
    answer, prompt, image = q
    options = make_options(await _topic_items(session, topic), answer)
    label = {"skin": "Skin", "agent": "Agent", "weapon": "Weapon"}[topic]
    embed = discord.Embed(title=f"🎲 Valorant Trivia · {label}", description=prompt, color=RED)
    if image:
        embed.set_image(url=image)
    embed.set_footer(text="First correct answer scores. New question drops after each answer.")
    return embed, TriviaView(answer, options)


class TriviaView(discord.ui.View):
    def __init__(self, answer: str, options: list[str]):
        super().__init__(timeout=45)
        self.answer = answer
        self.done = False
        for opt in options:
            self.add_item(_TriviaButton(opt, opt == answer))

    async def on_timeout(self):
        # Nobody answered in time -> lock the buttons and let the chain die.
        for c in self.children:
            c.disabled = True


class _TriviaButton(discord.ui.Button):
    def __init__(self, label: str, correct: bool):
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary)
        self.correct = correct

    async def callback(self, interaction: discord.Interaction):
        view: TriviaView = self.view
        if view.done:  # someone already answered this one
            await interaction.response.defer()
            return
        view.done = True
        for c in view.children:
            c.disabled = True
            if c.correct:
                c.style = discord.ButtonStyle.success
            elif c is self:
                c.style = discord.ButtonStyle.danger

        scores = load_scores()
        e = apply_result(scores, str(interaction.user.id),
                         interaction.user.display_name, self.correct)
        save_scores(scores)

        who = interaction.user.display_name
        if self.correct:
            streak = e["streak"]
            flair = f"  🔥 **{streak} in a row!**" if streak >= 3 else ""
            verdict = f"✅ **{who}** got it — **{e['correct']} pts**{flair}"
        else:
            verdict = f"❌ **{who}** missed — it was **{view.answer}**"

        await interaction.response.edit_message(content=verdict, view=view)
        view.stop()

        # Stack the next question so play keeps rolling.
        nxt = await new_question(interaction.client.session)
        if nxt:
            embed, nview = nxt
            await interaction.followup.send(embed=embed, view=nview)
