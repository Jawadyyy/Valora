"""Diagnostics that run outside Discord, so a failure points at one thing.

    python debug_auth.py           test an ssid cookie end to end
    python debug_auth.py --daily   dry-run the alert / daily-shop job

The cookie is read with getpass (not echoed) and only ever printed truncated.
"""

import asyncio
import getpass
import sys

import aiohttp

import valorant_bot_simple as bot


def short(s, n=12):
    return f"{s[:n]}...<{len(s)} chars>" if s and len(s) > n else s


async def daily_dry_run():
    """Run what the 00:05 UTC job would do, printing instead of DMing.

    Uses the real stored users and a real session, so this exercises the actual
    alert-matching path - unlike calling daily_job() directly, which would hit a
    None session and swallow the error.
    """
    users = bot.load_users()
    if not users:
        raise SystemExit("No linked users. Run /login in Discord first.")

    async with aiohttp.ClientSession() as session:
        for discord_id, user in users.items():
            alerts = user.get("alerts") or []
            wants_shop = bool(user.get("daily_shop"))
            print(f"\n--- user {discord_id} ---")
            print(f"    alerts: {alerts or 'none'}")
            print(f"    daily shop: {'on' if wants_shop else 'off'}")
            if not alerts and not wants_shop:
                print("    -> nothing to do, job would skip this user")
                continue

            try:
                sf = await bot.fetch_storefront(session, user)
            except bot.AuthError as e:
                print(f"    -> would DM: \"Couldn't check your shop: {e}\"")
                continue

            in_shop = await bot.store_skin_names(session, sf)
            print(f"    in shop today: {', '.join(in_shop)}")

            if alerts:
                lowered = {n.lower() for n in in_shop}
                hits = [a for a in alerts if a.lower() in lowered]
                print(f"    -> would DM alert: {hits}" if hits
                      else "    -> no alert matches today")
            if wants_shop:
                print(f"    -> would DM the shop ({len(in_shop)} embeds)")

    print("\nDry run complete. Nothing was sent.")


async def main():
    print("Paste your Riot cookie header (input is hidden).")
    print("Get it: sign in at https://account.riotgames.com with 'Remember me'")
    print("ticked -> F12 -> Network tab -> visit https://auth.riotgames.com ->")
    print("click the auth.riotgames.com request -> Request Headers -> cookie:\n")
    raw = getpass.getpass("cookie header (hidden): ")
    if not raw.strip():
        raise SystemExit("nothing pasted")

    cookies = bot.parse_cookies(raw)
    print(f"\nparsed {len(cookies)} cookie(s): {', '.join(sorted(cookies)) or '<none>'}")
    if not cookies.get("ssid"):
        print("\n    --> FAILED: no 'ssid' in what you pasted.")
        print("        You likely copied the wrong header or only part of the line.")
        print("        It must contain 'ssid=' somewhere.")
        return
    print(f"    ssid: {short(cookies['ssid'])}")
    if len(cookies["ssid"]) < 100:
        print("    !! that ssid looks short - it's usually 600+ chars.")
        print("       Probable truncated copy: double-click the value, Ctrl+A, Ctrl+C.")

    # --- raw redirect, so a failure shows what Riot actually sent back --------
    async with aiohttp.ClientSession(headers={"User-Agent": bot.RIOT_UA}) as s:
        async with s.get(bot.REAUTH_URL, cookies=cookies,
                         allow_redirects=False) as r:
            print(f"\n[1] GET authorize -> HTTP {r.status}")
            location = r.headers.get("Location", "")
            print(f"    Location: {location[:90] or '<none>'}")
            print(f"    cookies Riot sent back: {', '.join(r.cookies) or 'none'}")

    if not bot.token_from_uri(location):
        print("\n    --> FAILED: no access_token in the redirect.")
        if not location:
            print("        No redirect at all - Riot rejected the cookies outright.")
        elif "login" in location:
            print("        Bounced to the login page = cookies expired or invalid.")
            print("        Most likely: 'Remember me' wasn't ticked when you signed in.")
        elif "error=" in location:
            print(f"        Riot returned an error in the fragment: {location[-60:]}")
        print("\n        Sign out of Riot fully, sign back in WITH 'Remember me',")
        print("        and copy the cookie header again.")
        return

    # --- full flow through the bot's own code path ---------------------------
    print("\n[2] running the bot's reauth()...")
    try:
        tokens = await bot.reauth(cookies)
    except bot.AuthError as e:
        print(f"    --> FAILED: {e}")
        return

    print("    --> SUCCESS")
    print(f"        access_token       {short(tokens['access_token'])}")
    print(f"        entitlements_token {short(tokens['entitlements_token'])}")
    print(f"        puuid              {tokens['puuid']}")
    rotated = [k for k, v in tokens["cookies"].items() if cookies.get(k) != v]
    print(f"        cookies rotated    {rotated or 'none'}")

    # --- storefront ----------------------------------------------------------
    print("\n[3] fetching the storefront...")
    async with aiohttp.ClientSession() as s:
        try:
            sf = await bot.fetch_storefront(s, dict(tokens))
        except bot.AuthError as e:
            print(f"    --> FAILED: {e}")
            return
        offers = bot.parse_store(sf)
        print(f"    --> {len(offers)} daily offers")
        for o in offers:
            skin = await bot.get_skin_level(s, o["uuid"])
            print(f"        {o['price']:>5} VP  {skin['displayName']}")
        nm = bot.parse_night_market(sf)
        print(f"    night market: {len(nm)} items" if nm else "    night market: not active")

    print("\nAll good. Use this same cookie with /login in Discord.")


if __name__ == "__main__":
    asyncio.run(daily_dry_run() if "--daily" in sys.argv else main())
