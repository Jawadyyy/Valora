# valoskin-bot

A Valorant Discord bot for the Team HEX community. Started as a store viewer,
now covers store, item/agent/map reference, live rank & match stats, auto rank
roles, an esports feed, and a trivia game — all multi-user.

**Three data sources, three trust levels:**
- **valorant-api.com** — static assets (skins, agents, maps). No key, no login, no risk.
- **Henrik API** — rank, stats, esports, crosshair. A free API key, keyed by Riot ID (no cookie).
- **In-game API** — your personal store/night-market. Needs your Riot session cookie (per-user).

```
/login-help          how to get your Riot cookie
/login <cookies>     link account (once, ephemeral)
/store               today's 4 skins + prices
/night-market        discounts, when active
/alert <skin>        DM me when this skin hits my shop (autocompletes)
/alerts              list what I'm tracking
/alert-remove <skin> stop tracking
/daily-shop <on/off> DM me my shop every day
/logout              delete stored session and alerts

--- reference (no login, works for anyone) ---
/skin <name>         image, tier, levels, chromas
/agent <name>        role + abilities
/weapon <name>       damage, fire rate, wall pen, cost
/map <name>          top-down layout + callouts
/buddy /spray /card /title <name>   cosmetic lookup
/random-agent        pick a random agent
/random-loadout      random skins for a full buy
/trivia [topic]      endless quiz — new question after each answer, streaks + points
/trivia-leaderboard  top trivia players on the server

--- Henrik (free API key, riot-ID keyed, no cookie) ---
/rank <name> <tag>   rank, RR, peak + K/D, HS%, ACS, W-L (recent comp)
/matches [name] [tag]   last 5 comp games: map, agent, KDA, ACS, HS%, W/L
/mmr-history [name] [tag]  RR over time — sparkline, recent changes, current rank
/leaderboard [region] [tier]  top 15 ranked players, optional tier filter
/crosshair <code>    render a crosshair image from its share code
/featured            current bundle + VP/PKR price + items
/status              Valorant incidents per region, with detail

--- premier ---
/premier <name> [tag]   roster, rank division (Open→Invite), league, W-L, Premier Score vs 600
/premier-standings [region] [league]  top teams in a region, optional league filter
/premier-conferences [region]  list the leagues (conferences) in a region + their cities

--- rank roles (auto-assigned Discord roles) ---
/link-riot <n> <tag> link your Riot ID (also powers stats/balancer)
/rank-role           refresh your rank role now
/setup-rank-roles    admin: create the tier roles (needs Manage Roles)
/balance             split your voice channel into two rank-balanced teams

--- esports ---
/esports [league]    upcoming & live matches (filter: Americas/EMEA/Pacific/…)
/live                matches in progress right now
/results [league]    recent results with scores
/follow-team <team>  DM me when that team's match finishes (per-user)
/unfollow-team       stop following
/follows             list the teams I follow
```

Python 3.9+ · discord.py · unofficial Riot API + Henrik + valorant-api ·
auto-detects region (na/eu/ap/kr) · 41 slash commands · self-checking test suite
(`python test_valorant_bot.py`).

**No passwords.** Riot's password endpoint now demands an hCaptcha token, so the
bot uses cookie reauth instead: you sign in on Riot's own login page and give the
bot the resulting session cookie. It stores no password, works with 2FA, and
rotates the cookie on every refresh to stay linked.

- **[LOGIN_GUIDE.html](LOGIN_GUIDE.html)** — the picture guide to send teammates
  ([hosted copy](https://claude.ai/code/artifact/3a2fa014-5bde-415b-8906-f411bad6c821))
- [SETUP.md](SETUP.md) — install, run, get the cookie, troubleshoot
