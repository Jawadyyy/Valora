# Setup

## 1. Discord bot

1. https://discord.com/developers/applications -> New Application
2. Bot tab -> Reset Token -> copy it
3. OAuth2 -> URL Generator -> scopes `bot` + `applications.commands`,
   permissions `Send Messages` + `Embed Links` -> open the generated URL, invite
   to the Team HEX server

## 2. Local

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Put the bot token in `.env`. Set `DEV_GUILD_ID` to your server's id while
developing — global slash-command sync takes up to an hour, guild sync is instant.

## 3. Run

```bash
python valorant_bot_simple.py
```

## Using it

| Command | What |
|---|---|
| `/login-help` | How to get your `ssid` cookie |
| `/login <ssid>` | Link Riot account. Ephemeral — only you see it. |
| `/store` | Today's 4 skins + VP prices |
| `/night-market` | Discounts, if the market is live |
| `/alert <skin>` | DM when that skin appears in your shop. Name autocompletes. |
| `/alerts` | List what you're tracking |
| `/alert-remove <skin>` | Stop tracking one |
| `/daily-shop <on/off>` | DM your shop every day after reset |
| `/logout` | Wipe your stored session and alerts |

**Reference commands** (no login — anyone can use these):

| Command | What |
|---|---|
| `/skin <name>` | Image, content tier, level + chroma count |
| `/agent <name>` | Role + abilities (valorant-api has no cost/cooldown data) |
| `/weapon <name>` | Cost, fire rate, magazine, wall pen, damage falloff |
| `/map <name>` | Top-down layout image + callout names |
| `/buddy` `/spray` `/card` `/title` `<name>` | Cosmetic lookup |
| `/random-agent` | Random agent |
| `/random-loadout` | A random skin for each weapon in a full buy |
| `/trivia [topic]` | Endless button quiz — a new question drops after each answer; points + streaks tracked. Topic: mixed (default), skin, agent, weapon |
| `/trivia-leaderboard` | Server ranking by points, best streak, accuracy |

Trivia scores persist in `trivia_scores.json` (gitignored).

These pull from valorant-api.com — no key, no login, no risk. They live in
`valorant_static.py`, separate from the store/auth code.

**Henrik commands** (need a free `HENRIK_API_KEY` in `.env`; riot-ID keyed, no cookie):

| Command | What |
|---|---|
| `/rank <name> <tag> [region]` | Rank, RR, peak + K/D, HS%, ACS, W-L from recent comp games |
| `/matches [name] [tag] [region]` | Last 5 competitive games — map, agent, KDA, ACS, HS%, W/L |
| `/mmr-history [name] [tag] [region]` | RR over time — unicode sparkline, recent RR changes, current rank |
| `/leaderboard [region] [tier]` | Top 15 ranked players in a region, optional tier filter (cached 5 min) |
| `/crosshair <code>` | Render a crosshair from its in-game share code |
| `/featured` | Current bundle — VP + PKR estimate + item list |
| `/status [region]` | Riot incidents/maintenance with detail (which server, which agent) |

`/matches` and `/mmr-history` default to your `/link-riot` account when you omit
name/tag; pass a name + tag (+ region) to look up anyone. Both live in `stats.py`.

Get the key from [Henrik's docs](https://docs.henrikdev.xyz) (their Discord).
Without the key these four reply asking you to set it; the rest of the bot works
regardless. Live in `henrik.py`.

PKR is a rough estimate — tune `VP_TO_PKR` in `henrik.py` to the current rate.

**Esports commands** (also use `HENRIK_API_KEY`; live in `esports.py`):

| Command | What |
|---|---|
| `/esports [league]` | Upcoming & live matches; filter by league (Americas, EMEA, Pacific, Game Changers, …) |
| `/live` | Matches in progress now |
| `/results [league]` | Recent results with scores |
| `/follow-team <team>` | DMs *you* when that team's match finishes — per-user, private |
| `/unfollow-team <team>` · `/follows` | Manage your own follows |

A background job checks every 30 min and DMs each follower their teams' new
results. Follows are per-user (`esports_follows.json`, gitignored): different
people follow different teams independently.

**Premier commands** (`premier.py`; use `HENRIK_API_KEY`, no cookie):

| Command | What |
|---|---|
| `/premier <name> [tag]` | Team roster, division/conference, W-L, and Premier Score vs the 625 playoff line. With a tag it's an exact lookup; without, it searches the exact team name. |
| `/premier-standings [region]` | Top 15 Premier teams in a region by score |

Henrik's Premier data covers rosters, standings, W-L and score — **no future
match schedule is exposed**, so there are no match reminders (the history
endpoint only returns past matches). Those are intentionally not built.

**Auto rank roles** (`rankroles.py`; needs `HENRIK_API_KEY` + the **Manage Roles** permission):

| Command | What |
|---|---|
| `/link-riot <name> <tag> [region]` | Link your Riot ID (also used by stats & balancer) |
| `/rank-role` | Refresh your rank role right now |
| `/setup-rank-roles` | **Admin** — create the 9 tier roles (Iron→Radiant) on this server |
| `/balance` | Split your current voice channel into two rank-balanced teams (uses linked members' elo) |

Setup, once per server:
1. Give the bot **Manage Roles** (Server Settings → Roles → the bot's role).
2. Drag the bot's role **above** where the tier roles will sit.
3. Run `/setup-rank-roles`. Members then `/link-riot` to get their role.
4. The bot re-checks everyone's rank **weekly** and updates roles automatically.

Links + role config persist in `rankroles.json` (gitignored). Rank data is
per-Riot-ID via Henrik — no cookie needed, so anyone can use it.

**Not available:** arbitrary skin prices / collection value / savings math —
Riot removed the upstream Henrik used, so only the *current featured bundle*
carries prices (shown in `/featured`).

Alerts and daily DMs run off a job at **00:05 UTC**, just after Valorant's shop
rotates. The bot must be running at that moment — it doesn't backfill missed days.

Full walkthrough including how to grab the cookie: [TESTING.md](TESTING.md).

## Read this before you ask 5 people to /login

The bot never receives a password — Riot's password endpoint requires an
hCaptcha token, so users sign in on Riot's real page and hand over the resulting
`ssid` session cookie instead. Still worth being straight with people:

- An `ssid` **is** a credential. Whoever holds it can act as that Riot account
  until it expires. It's weaker than a password (revocable by signing out, and
  it expires on its own) but it is not nothing.
- `valorant_users.json` is gitignored. Keep it that way. Never paste it anywhere.
- Riot has stated that unauthorized third-party apps pulling client-hidden data
  get **players** banned. Store contents are visible in-client, so a store viewer
  is a softer case — but the exposure is on your teammates' accounts, not yours.
  Tell them before they link.
- A user can revoke at any time: `/logout` here, or sign out of Riot in their
  browser, which kills the cookie everywhere.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Slash commands missing | Guild not synced — set `DEV_GUILD_ID`, or wait for global sync |
| `login` rejects the cookie | Expired, truncated on copy, or grabbed without "Remember me" ticked at sign-in. Get a fresh `ssid`. |
| "Invalid Request" on auth.riotgames.com | Expected — that page needs OAuth params. You're only there to read its cookies. |
| No `ssid` cookie listed | Not signed in, or "Remember me" wasn't ticked. |
| `store` 401 loop | `ssid` died. Run `/logout`, then `/login` with a new cookie. |
| `store` 400 | Stale `X-Riot-ClientVersion`. Restart the bot to refetch. |
| `store` 404 | Region mis-detected. Region auto-detects per account (na/eu/ap/kr); re-run `/login` to refresh it. |
