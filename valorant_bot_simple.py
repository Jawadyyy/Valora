"""
Valorant Store Discord Bot - Team HEX
See SCOPE.md for requirements. This file is a skeleton: signatures and flow are
laid out, the bodies are TODO.

AUTH: cookie reauth, NOT username/password.

Riot's password endpoint (PUT /api/v1/authorization) now requires an hCaptcha
token in a riot_identity body. Sending the old flat {"type":"auth","username",
"password"} body returns {"type":"auth","error":"auth_failure"} no matter how
correct the credentials are. We do not solve captchas. Instead the user logs in
on Riot's real login page in their own browser and hands us the resulting `ssid`
cookie, which we exchange for tokens:

  1. GET https://auth.riotgames.com/authorize
         ?redirect_uri=https%3A%2F%2Fplayvalorant.com%2Fopt_in
         &client_id=play-valorant-web-prod
         &response_type=token%20id_token&nonce=1&scope=account%20openid
     Cookie: ssid=<user's cookie>       allow_redirects=False
     -> 30x whose Location fragment holds access_token. If the fragment has no
        access_token (or we get a login page), the ssid is dead.
     -> the response also Set-Cookie's a FRESH ssid. Persist it: doing so on
        every reauth keeps a regularly-used account linked indefinitely.
        Untouched, an ssid dies in about a week.
  2. POST https://entitlements.auth.riotgames.com/api/token/v1
     Authorization: Bearer <access_token>  -> entitlements_token
  3. GET https://auth.riotgames.com/userinfo
     Authorization: Bearer <access_token>  -> "sub" == puuid

No password is ever received, transmitted, or stored. 2FA accounts work fine,
because the human does the interactive login themselves.

Storefront (verified live 2026-08): POST https://pd.<shard>.a.pvp.net/store/v3/
storefront/<puuid> with an empty JSON body. The old v2 GET now 404s. Required
headers - ALL FOUR, or it 400s:
     Authorization: Bearer <access_token>
     X-Riot-Entitlements-JWT: <entitlements_token>
     X-Riot-ClientVersion:  from valorant-api.com/v1/version -> riotClientVersion
     X-Riot-ClientPlatform: base64 of the fixed PC-client JSON (CLIENT_PLATFORM)

<shard> comes from the geo service (get_region): affinity na/eu/asia/kr maps to
shard na/eu/ap/kr. Using the wrong shard 404s.

Storefront response shape (v3 - same keys we read as v2):
  SkinsPanelLayout.SingleItemOffers          -> 4 skin-level UUIDs (daily store)
  SkinsPanelLayout.SingleItemStoreOffers[].Cost -> {currencyUuid: price}
  BonusStore.BonusStoreOffers[]              -> night market
      .Offer.Cost           -> original price
      .DiscountCosts        -> discounted price
      .DiscountPercent      -> int
      .Offer.OfferID        -> skin LEVEL uuid, resolve via
                               https://valorant-api.com/v1/weapons/skinlevels/<uuid>

VP currency uuid: 85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741
"""

import asyncio
import datetime
import json
import logging
import base64
import os
import pathlib
import urllib.parse

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

log = logging.getLogger("valoskin")

load_dotenv()

DEV_GUILD_ID = os.getenv("DEV_GUILD_ID") or None

USERS_FILE = pathlib.Path(__file__).with_name("valorant_users.json")
# Valorant PD shards are na / eu / ap / kr (NOT the League "na1" routing value).
# Each user's shard is detected from their account - see get_region. This is
# only the fallback if detection ever fails.
DEFAULT_REGION = "na"
VAPI_URL = "https://valorant-api.com/v1"
VP_UUID = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"

REAUTH_URL = (
    "https://auth.riotgames.com/authorize"
    "?redirect_uri=https%3A%2F%2Fplayvalorant.com%2Fopt_in"
    "&client_id=play-valorant-web-prod"
    "&response_type=token%20id_token"
    "&nonce=1"
    "&scope=account%20openid"
)
RIOT_UA = "RiotClient/63.0.9.4909983.4789131 rso-auth (Windows;10;;Professional, x64)"

# Storefront now REQUIRES this header or it 400s. It's base64 of a fixed JSON
# blob describing a Windows PC client:
#   {"platformType":"PC","platformOS":"Windows",
#    "platformOSVersion":"10.0.19042.1.256.64bit","platformChipset":"Unknown"}
CLIENT_PLATFORM = (
    "eyJwbGF0Zm9ybVR5cGUiOiJQQyIsInBsYXRmb3JtT1MiOiJXaW5kb3dzIiwicGxhdGZvcm1P"
    "U1ZlcnNpb24iOiIxMC4wLjE5MDQyLjEuMjU2LjY0Yml0IiwicGxhdGZvcm1DaGlwc2V0Ijoi"
    "VW5rbm93biJ9"
)

# Riot's geo service returns a granular "affinity" (na/latam/br/eu/ap/asia/
# sea/sea2/kr); the pd storefront host only has four "shards". Map one to the
# other. SEA affinities all live on the ap shard - pd.sea2 does not exist.
AFFINITY_TO_SHARD = {
    "na": "na", "latam": "na", "br": "na",
    "eu": "eu",
    "ap": "ap", "asia": "ap", "sea": "ap", "sea2": "ap", "sea1": "ap",
    "kr": "kr",
}
VALID_SHARDS = {"na", "eu", "ap", "kr"}


# --------------------------------------------------------------------------
# storage - flat json, {discord_user_id: {ssid, access_token,
#                                         entitlements_token, puuid}}
# no passwords, by design - see the module docstring
# --------------------------------------------------------------------------

def load_users() -> dict:
    """Return stored users, {} if file missing or corrupt."""
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_users(users: dict) -> None:
    """Write users atomically (tmp file + replace) so a crash can't truncate it."""
    tmp = USERS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, USERS_FILE)


# --------------------------------------------------------------------------
# riot auth
# --------------------------------------------------------------------------

class AuthError(Exception):
    """Raised for a dead/invalid ssid cookie, or Riot being unreachable."""


def parse_cookies(raw: str) -> dict[str, str]:
    """Turn whatever the user pasted into a cookie dict.

    Accepts a full request `cookie:` header - `tdid=..; did=..; ssid=..; clid=..`
    - which is what we ask for, because reauth with the whole set survives ~3
    weeks against ~1 week for ssid alone. Also accepts a bare ssid value so a
    user who grabbed only that still gets something that works.
    """
    s = raw.strip().strip('"').strip("'")
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()

    if "=" not in s:
        # A bare value pasted with no name - can only be the ssid.
        return {"ssid": s} if s else {}

    out = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        # Drop Set-Cookie attributes if someone pasted a Set-Cookie line.
        if name.lower() in {"path", "domain", "expires", "max-age", "samesite",
                            "secure", "httponly", "version"}:
            continue
        out[name] = value.strip()
    return out


async def reauth(cookies: dict[str, str]) -> dict:
    """Exchange Riot session cookies for tokens. See the module docstring.

    Returns {access_token, entitlements_token, puuid, cookies} where cookies is
    the set updated with anything Riot rotated.
    Raises AuthError with a user-safe message.
    """
    if not cookies.get("ssid"):
        raise AuthError(
            "No `ssid` found in what you pasted. Copy the whole `cookie:` header "
            "value — see `/login-help`."
        )
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": RIOT_UA}) as session:
            # Step 1 - redeem the cookies. Must NOT follow the redirect: the token
            # lives in the Location fragment and aiohttp drops fragments on follow.
            async with session.get(
                REAUTH_URL, cookies=cookies, allow_redirects=False
            ) as r:
                location = r.headers.get("Location", "")
                # Riot rotates cookies on reauth - keep whatever it sent back.
                fresh = dict(cookies)
                for name, morsel in r.cookies.items():
                    fresh[name] = morsel.value

            access_token = token_from_uri(location)
            if not access_token:
                raise AuthError(
                    "Those cookies didn't work — expired, truncated when copied, or "
                    "captured without “Remember me” ticked. See `/login-help` and "
                    "grab a fresh set."
                )

            # Step 2 - entitlements token.
            async with session.post(
                "https://entitlements.auth.riotgames.com/api/token/v1",
                headers={"Authorization": f"Bearer {access_token}"},
                json={},
            ) as r:
                entitlements_token = (await r.json()).get("entitlements_token")
            if not entitlements_token:
                raise AuthError("Could not fetch entitlements token. Try again later.")

            # Step 3 - puuid.
            async with session.get(
                "https://auth.riotgames.com/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as r:
                puuid = (await r.json()).get("sub")
            if not puuid:
                raise AuthError("Could not fetch account id. Try again later.")

            # Step 4 - region/shard, so we query the right storefront host.
            # Token is fresh here, so detection should succeed; fall back only
            # if Riot's geo service is down.
            region = await get_region(session, access_token) or DEFAULT_REGION

            return {
                "access_token": access_token,
                "entitlements_token": entitlements_token,
                "puuid": puuid,
                "cookies": fresh,
                "region": region,
            }
    except aiohttp.ClientError:
        raise AuthError("Could not reach Riot. Check the connection and try again.")


async def get_region(session: aiohttp.ClientSession, access_token: str) -> str | None:
    """Detect the account's Valorant shard (na/eu/ap/kr) so we hit the right
    storefront host. Riot's geo service returns a signed JWT whose `affinity`
    claim we map to a shard. We read the claim, we don't verify the signature.

    Returns None if the token was rejected or the response wasn't a JWT - the
    caller decides whether to fall back or retry with a fresh token."""
    async with session.get(
        "https://riot-geo.pas.si.riotgames.com/pas/v1/service/chat",
        headers={"Authorization": f"Bearer {access_token}"},
    ) as r:
        if r.status != 200:
            return None
        jwt = (await r.text()).strip()
    return region_from_pas(jwt)


def region_from_pas(jwt: str) -> str | None:
    """Pull `affinity` out of the PAS JWT payload and map it to a shard.
    None if malformed. Unknown affinities pass through unmapped."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        aff = json.loads(base64.urlsafe_b64decode(payload)).get("affinity")
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    if not aff:
        return None
    return AFFINITY_TO_SHARD.get(aff, aff)


def token_from_uri(uri: str) -> str | None:
    """Pull access_token out of a redirect URI fragment.

    uri looks like: https://playvalorant.com/opt_in#access_token=...&id_token=...
    Returns None for an empty uri, a fragment-less uri, or an error redirect
    back to the login page - all of which mean the ssid is no good.
    """
    fragment = urllib.parse.urlparse(uri).fragment
    return urllib.parse.parse_qs(fragment).get("access_token", [None])[0]


# --------------------------------------------------------------------------
# valorant api
# --------------------------------------------------------------------------

_client_version: str | None = None
_skin_cache: dict[str, dict] = {}


async def get_client_version(session: aiohttp.ClientSession) -> str:
    """Current riotClientVersion, needed for the storefront X-Riot-ClientVersion
    header. Cached for the process lifetime."""
    global _client_version
    if _client_version is None:
        async with session.get(f"{VAPI_URL}/version") as r:
            _client_version = (await r.json())["data"]["riotClientVersion"]
    return _client_version


def parse_store(storefront: dict) -> list[dict]:
    """Pure: daily store -> [{uuid, price}]. VP price per skin-level offer."""
    panel = storefront.get("SkinsPanelLayout", {})
    offers = []
    for offer in panel.get("SingleItemStoreOffers", []):
        offers.append({
            "uuid": offer.get("OfferID"),
            "price": offer.get("Cost", {}).get(VP_UUID),
        })
    return offers


def parse_night_market(storefront: dict) -> list[dict]:
    """Pure: night market -> [{uuid, original, discounted, percent}].

    Empty list if the market isn't running (no BonusStore). DiscountPercent comes
    straight from the API - never computed here.
    """
    bonus = storefront.get("BonusStore")
    if not bonus:
        return []
    items = []
    for entry in bonus.get("BonusStoreOffers", []):
        offer = entry.get("Offer", {})
        items.append({
            "uuid": offer.get("OfferID"),
            "original": offer.get("Cost", {}).get(VP_UUID),
            "discounted": entry.get("DiscountCosts", {}).get(VP_UUID),
            "percent": entry.get("DiscountPercent"),
        })
    return items


def _persist_tokens(user: dict) -> None:
    """Write refreshed tokens back to the users file. Matched on puuid, which is
    stable across re-auth (the discord id isn't passed in here).

    The rotated cookies are written too - that is what keeps a regularly-used
    account linked past the few weeks a static cookie set survives.
    """
    users = load_users()
    for entry in users.values():
        if entry.get("puuid") == user.get("puuid"):
            entry["access_token"] = user["access_token"]
            entry["entitlements_token"] = user["entitlements_token"]
            entry["cookies"] = user["cookies"]
            entry["region"] = user.get("region", entry.get("region"))
    save_users(users)


async def fetch_storefront(session: aiohttp.ClientSession, user: dict) -> dict:
    """GET /store/v2/storefront/{puuid} against the user's own region shard.

    On 401 -> reauth with the stored cookies once, persist the new tokens, retry.
    Second 401 -> raise AuthError telling the user to /login again.
    """
    version = await get_client_version(session)

    # Re-detect region if it's missing OR not a real pd shard (e.g. an old
    # "sea2" saved before the affinity map covered it - pd.sea2 doesn't exist).
    # Reauth refreshes the token AND returns a corrected region in one go.
    if user.get("region") not in VALID_SHARDS:
        user.update(await reauth(user["cookies"]))
        _persist_tokens(user)

    region = user.get("region") if user.get("region") in VALID_SHARDS else DEFAULT_REGION
    url = f"https://pd.{region}.a.pvp.net/store/v3/storefront/{user['puuid']}"

    async def _get():
        headers = {
            "Authorization": f"Bearer {user['access_token']}",
            "X-Riot-Entitlements-JWT": user["entitlements_token"],
            "X-Riot-ClientVersion": version,
            "X-Riot-ClientPlatform": CLIENT_PLATFORM,
        }
        # v3 storefront is a POST with an empty body; v2 GET was removed (404s).
        async with session.post(url, headers=headers, json={}) as r:
            body = await r.json() if r.status == 200 else None
            return r.status, body

    status, body = await _get()

    # Riot returns 400 (NOT 401) for an expired access token. Tokens die after
    # ~1h, so without covering 400 here every user's store silently breaks an
    # hour after they link - which looks like "only the last person to log in
    # works". Treat both as stale -> reauth with stored cookies, persist, retry.
    if status in (400, 401):
        tokens = await reauth(user["cookies"])  # raises AuthError
        user.update(tokens)
        _persist_tokens(user)
        status, body = await _get()
        if status in (400, 401):
            raise AuthError("Your session expired and refresh failed. Run `/login` again.")

    if status != 200:
        raise AuthError(f"Store unavailable (Riot returned {status}). Try again later.")
    return body


async def get_skin_level(session: aiohttp.ClientSession, uuid: str) -> dict:
    """GET /weapons/skinlevels/{uuid} from valorant-api.com -> displayName, displayIcon.

    Static data, same for everyone -> cache it in a module-level dict so 50 users
    checking the same daily store make 4 requests, not 200.
    """
    if uuid in _skin_cache:
        return _skin_cache[uuid]
    try:
        async with session.get(f"{VAPI_URL}/weapons/skinlevels/{uuid}") as r:
            data = (await r.json()).get("data") or {}
        skin = {"displayName": data.get("displayName") or "Unknown skin",
                "displayIcon": data.get("displayIcon")}
    except aiohttp.ClientError:
        # Don't cache a failed lookup - let it retry next time.
        return {"displayName": "Unknown skin", "displayIcon": None}
    _skin_cache[uuid] = skin
    return skin


_skin_names: list[str] = []


async def all_skin_names(session: aiohttp.ClientSession) -> list[str]:
    """Every skin display name, for /alert autocomplete. One fetch per process."""
    global _skin_names
    if not _skin_names:
        try:
            async with session.get(f"{VAPI_URL}/weapons/skins") as r:
                data = (await r.json()).get("data") or []
            # Dedupe and drop the default "Standard" variants nobody alerts on.
            names = {s["displayName"] for s in data
                     if s.get("displayName") and "Standard" not in s["displayName"]}
            _skin_names = sorted(names)
        except (aiohttp.ClientError, KeyError):
            return []
    return _skin_names


# --------------------------------------------------------------------------
# embeds - shared by the slash commands and the daily DM job
# --------------------------------------------------------------------------

async def build_store_embeds(session: aiohttp.ClientSession, storefront: dict) -> list[discord.Embed]:
    embeds = []
    for offer in parse_store(storefront):
        skin = await get_skin_level(session, offer["uuid"])
        price = offer["price"]
        e = discord.Embed(
            title=skin["displayName"],
            description=f"{price} VP" if price is not None else "Price unavailable",
            color=0xFF4655,
        )
        if skin["displayIcon"]:
            e.set_thumbnail(url=skin["displayIcon"])
        embeds.append(e)
    return embeds


async def store_skin_names(session: aiohttp.ClientSession, storefront: dict) -> list[str]:
    """Display names currently in the daily store. Used to match alerts."""
    names = []
    for offer in parse_store(storefront):
        skin = await get_skin_level(session, offer["uuid"])
        names.append(skin["displayName"])
    return names


# --------------------------------------------------------------------------
# bot
# --------------------------------------------------------------------------

class ValoBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        daily_job.start()
        esports.start_jobs()
        rankroles.start_jobs()

    async def close(self):
        daily_job.cancel()
        esports.results_job.cancel()
        rankroles.recheck_job.cancel()
        if self.session:
            await self.session.close()
        await super().close()


bot = ValoBot()

# Static-data commands (skins/agents/weapons/maps/cosmetics/randoms) live in
# their own module - no auth, no shared state with the store code.
import valorant_static
valorant_static.register(bot)

# Henrik-API commands (rank/crosshair/featured/status) - riot-ID keyed, no cookie.
import henrik
henrik.register(bot)

# Esports (schedule/results/live/follow-team) - Henrik esports schedule.
import esports
esports.register(bot)

# Auto rank roles (link-riot / setup-rank-roles / weekly recheck) - Henrik mmr.
import rankroles
rankroles.register(bot)


@bot.tree.command(description="Link your account with your Riot cookies (run /login-help first)")
@app_commands.describe(cookies="Your Riot cookie header - never your password")
async def login(interaction: discord.Interaction, cookies: str):
    """MUST be ephemeral=True - these are session credentials and a
    non-ephemeral reply would expose them to the channel."""
    await interaction.response.defer(ephemeral=True)
    try:
        tokens = await reauth(parse_cookies(cookies))
    except AuthError as e:
        await interaction.followup.send(f"Login failed: {e}", ephemeral=True)
        return
    except Exception:
        # Never let an unexpected error leak internals (or the cookie).
        await interaction.followup.send("Login failed: something went wrong. Try again later.", ephemeral=True)
        return

    users = load_users()
    # Merge, don't replace: re-linking with a fresh cookie must not wipe the
    # user's alerts or their daily-shop opt-in.
    entry = users.get(str(interaction.user.id), {})
    entry.update(tokens)
    users[str(interaction.user.id)] = entry
    save_users(users)
    await interaction.followup.send(
        "Account linked. Try `/store`, `/bundles`, `/night-market`, or `/alert`.",
        ephemeral=True,
    )


@bot.tree.command(name="login-help", description="How to get your ssid cookie")
async def login_help(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Getting your Riot cookies**\n"
        "1. Open <https://account.riotgames.com> and sign in. "
        "**Tick “Remember me” before signing in** — without it Riot won't issue a "
        "lasting cookie. Captcha and 2FA are fine, you're on Riot's real page.\n"
        "2. Press **F12** and switch to the **Network** tab. Leave it open.\n"
        "3. In the same tab, go to <https://auth.riotgames.com>. You'll see "
        "**“Invalid Request” — that's expected**, ignore it.\n"
        "4. In the Network list, click the request named **auth.riotgames.com**.\n"
        "5. Scroll to **Request Headers**, find the **`cookie:`** row, and copy "
        "its whole value. It looks like `tdid=..; did=..; ssid=..; clid=..` and "
        "is long.\n"
        "6. Run `/login` and paste the whole thing.\n\n"
        "**Treat this like a password** — whoever holds it can act as your Riot "
        "account, and it bypasses 2FA. Only paste it into this bot's `/login`, "
        "never in chat. Signing out of Riot in your browser kills it everywhere. "
        "`/logout` deletes it here.",
        ephemeral=True,
    )


@bot.tree.command(description="Show your current Valorant store")
async def store(interaction: discord.Interaction):
    """Defer first - the API round-trips blow past Discord's 3s interaction limit."""
    await interaction.response.defer()
    user = load_users().get(str(interaction.user.id))
    if not user:
        await interaction.followup.send("You're not linked. Run `/login` first.")
        return
    try:
        storefront = await fetch_storefront(bot.session, user)
        embeds = await build_store_embeds(bot.session, storefront)
    except AuthError as e:
        await interaction.followup.send(str(e))
        return
    except Exception:
        log.exception("store failed for %s", interaction.user.id)
        await interaction.followup.send("Couldn't fetch your store. Try again later.")
        return

    if not embeds:
        await interaction.followup.send("Your store looks empty right now.")
        return
    await interaction.followup.send(content="Your daily store:", embeds=embeds)


@bot.tree.command(name="night-market", description="Show Night Market discounts")
async def night_market(interaction: discord.Interaction):
    await interaction.response.defer()
    user = load_users().get(str(interaction.user.id))
    if not user:
        await interaction.followup.send("You're not linked. Run `/login` first.")
        return
    try:
        storefront = await fetch_storefront(bot.session, user)
        if "BonusStore" not in storefront:
            await interaction.followup.send("Night Market isn't active right now.")
            return
        items = parse_night_market(storefront)[:8]
        embeds = []
        for item in items:
            skin = await get_skin_level(bot.session, item["uuid"])
            embed = discord.Embed(
                title=skin["displayName"],
                description=f"~~{item['original']} VP~~  →  **{item['discounted']} VP**  ({item['percent']}% off)",
                color=0xFF4655,
            )
            if skin["displayIcon"]:
                embed.set_thumbnail(url=skin["displayIcon"])
            embeds.append(embed)
    except AuthError as e:
        await interaction.followup.send(str(e))
        return
    except Exception:
        log.exception("night-market failed for %s", interaction.user.id)
        await interaction.followup.send("Couldn't fetch the Night Market. Try again later.")
        return

    if not embeds:
        await interaction.followup.send("Night Market is active but has no offers to show.")
        return
    await interaction.followup.send(content="Night Market:", embeds=embeds)


# The featured bundle is the same for everyone and needs no login, so it lives
# in /featured (Henrik, with prices + PKR) instead of a cookie-gated /bundles.


# --------------------------------------------------------------------------
# skin alerts
# --------------------------------------------------------------------------

async def skin_autocomplete(interaction: discord.Interaction, current: str):
    """Discord allows 25 choices max and wants a fast reply - substring match only."""
    names = await all_skin_names(bot.session)
    cur = current.lower()
    hits = [n for n in names if cur in n.lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in hits]


@bot.tree.command(description="Get a DM when a skin shows up in your shop")
@app_commands.describe(skin="Start typing a skin name")
@app_commands.autocomplete(skin=skin_autocomplete)
async def alert(interaction: discord.Interaction, skin: str):
    users = load_users()
    user = users.get(str(interaction.user.id))
    if not user:
        await interaction.response.send_message(
            "You're not linked. Run `/login` first.", ephemeral=True)
        return

    alerts = user.setdefault("alerts", [])
    if any(a.lower() == skin.lower() for a in alerts):
        await interaction.response.send_message(
            f"You're already tracking **{skin}**.", ephemeral=True)
        return
    alerts.append(skin)
    save_users(users)
    await interaction.response.send_message(
        f"Tracking **{skin}**. I'll DM you when it's in your shop.\n"
        "-# If my DM never arrives, check that this server allows DMs from members.",
        ephemeral=True,
    )


@bot.tree.command(name="alerts", description="List the skins you're tracking")
async def list_alerts(interaction: discord.Interaction):
    user = load_users().get(str(interaction.user.id)) or {}
    alerts = user.get("alerts") or []
    if not alerts:
        await interaction.response.send_message(
            "You're not tracking anything. Add one with `/alert`.", ephemeral=True)
        return
    listing = "\n".join(f"• {a}" for a in alerts)
    await interaction.response.send_message(
        f"**Tracking {len(alerts)} skin(s):**\n{listing}", ephemeral=True)


async def tracked_autocomplete(interaction: discord.Interaction, current: str):
    """Only offer skins this user actually tracks - no point listing all 700."""
    user = load_users().get(str(interaction.user.id)) or {}
    cur = current.lower()
    hits = [a for a in (user.get("alerts") or []) if cur in a.lower()][:25]
    return [app_commands.Choice(name=a, value=a) for a in hits]


@bot.tree.command(name="alert-remove", description="Stop tracking a skin")
@app_commands.autocomplete(skin=tracked_autocomplete)
async def alert_remove(interaction: discord.Interaction, skin: str):
    users = load_users()
    user = users.get(str(interaction.user.id)) or {}
    alerts = user.get("alerts") or []
    kept = [a for a in alerts if a.lower() != skin.lower()]
    if len(kept) == len(alerts):
        await interaction.response.send_message(
            f"You weren't tracking **{skin}**.", ephemeral=True)
        return
    user["alerts"] = kept
    save_users(users)
    await interaction.response.send_message(f"Stopped tracking **{skin}**.", ephemeral=True)


@bot.tree.command(name="daily-shop", description="DM me my shop every day")
@app_commands.describe(enabled="True to start daily DMs, False to stop")
async def daily_shop(interaction: discord.Interaction, enabled: bool):
    users = load_users()
    user = users.get(str(interaction.user.id))
    if not user:
        await interaction.response.send_message(
            "You're not linked. Run `/login` first.", ephemeral=True)
        return
    user["daily_shop"] = enabled
    save_users(users)
    await interaction.response.send_message(
        "Daily shop DMs **on**. First one arrives after the next shop reset."
        if enabled else "Daily shop DMs **off**.",
        ephemeral=True,
    )


# Valorant's shop rotates at 00:00 UTC. Run a few minutes after so Riot has
# actually swapped it over.
@tasks.loop(time=datetime.time(hour=0, minute=5, tzinfo=datetime.timezone.utc))
async def daily_job():
    users = load_users()
    for discord_id, user in list(users.items()):
        wants_shop = user.get("daily_shop")
        alerts = user.get("alerts") or []
        if not wants_shop and not alerts:
            continue
        try:
            await _notify_one(discord_id, user, wants_shop, alerts)
        except AuthError as e:
            # A dead cookie is that user's problem - tell them, keep going.
            await _try_dm(discord_id, f"Couldn't check your shop: {e}")
        except Exception:
            log.exception("daily job failed for %s", discord_id)
        # Space out the Riot calls rather than firing 50 at once.
        await asyncio.sleep(2)


@daily_job.before_loop
async def _wait_for_bot():
    # fetch_user and the shared session both need a connected client.
    await bot.wait_until_ready()


async def _notify_one(discord_id: str, user: dict, wants_shop: bool, alerts: list[str]):
    storefront = await fetch_storefront(bot.session, user)

    if alerts:
        in_shop = await store_skin_names(bot.session, storefront)
        lowered = {n.lower() for n in in_shop}
        hits = [a for a in alerts if a.lower() in lowered]
        if hits:
            await _try_dm(
                discord_id,
                "**Alert!** In your shop today: " + ", ".join(f"**{h}**" for h in hits),
            )

    if wants_shop:
        embeds = await build_store_embeds(bot.session, storefront)
        if embeds:
            await _try_dm(discord_id, "Your shop today:", embeds=embeds)


async def _try_dm(discord_id: str, content: str, embeds: list[discord.Embed] | None = None):
    """DMs fail routinely (closed DMs, left the server, deleted account).
    That's expected, not an error worth crashing the job over."""
    try:
        u = await bot.fetch_user(int(discord_id))
        await u.send(content=content, embeds=embeds or [])
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.info("could not DM %s", discord_id)


@bot.tree.command(description="Delete your stored credentials")
async def logout(interaction: discord.Interaction):
    users = load_users()
    if users.pop(str(interaction.user.id), None) is None:
        await interaction.response.send_message("You weren't logged in.", ephemeral=True)
        return
    save_users(users)
    await interaction.response.send_message(
        "Session and alerts deleted. Run `/login` to link again.", ephemeral=True)


if __name__ == "__main__":
    # Read at run time, not import time - the tests import this module and have
    # no bot token.
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Create a .env file next to this script "
            "containing DISCORD_TOKEN=<your bot token>. See SETUP.md."
        )
    bot.run(token)
