# Deploy on Oracle Cloud Free Tier (always-on, $0)

The bot needs 24/7 uptime, a persistent disk (the JSON files are its database),
and only **outbound** internet — no inbound web ports. An Oracle "Always Free"
Ampere ARM VM fits exactly.

## 1. Oracle account + VM  (you do this in the Oracle console)

1. Sign up at cloud.oracle.com. A card is required for identity verification;
   Always-Free resources are not charged. Pick a home region that still has ARM
   capacity (if VM creation says "out of capacity", try another region/time).
2. **Create instance:**
   - Image: **Ubuntu 22.04** (or 24.04)
   - Shape: **VM.Standard.A1.Flex** (Ampere ARM — the always-free one).
     1 OCPU / 6 GB RAM is plenty.
   - Add your **SSH public key** (generate with `ssh-keygen` if you don't have one).
3. **Networking:** leave defaults. The bot makes only outbound calls (Discord,
   Riot, Henrik), so you do **not** need to open any inbound ports. SSH (22) is
   already open for you to manage it.
4. Note the instance's **public IP**.

## 2. Connect + install

```bash
ssh ubuntu@<PUBLIC_IP>

sudo apt update
sudo apt install -y python3-venv python3-pip git

git clone https://github.com/Jawadyyy/Valora.git
cd Valora
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Requires Python 3.10+ (the code uses `X | None` type hints). Ubuntu 22.04 ships
3.10, 24.04 ships 3.12 — both fine.

## 3. Secrets — create `.env` on the server by hand

Never commit it. On the VM:

```bash
nano .env
```

```
DISCORD_TOKEN=your_bot_token
HENRIK_API_KEY=your_henrik_key
DEV_GUILD_ID=
```

Leave `DEV_GUILD_ID` empty for global command sync (up to ~1h to appear), or set
your server id for instant sync.

Sanity check it runs before making it a service:

```bash
.venv/bin/python valorant_bot_simple.py
```

You should see `logged in as ...`. Ctrl+C to stop, then set up the service.

## 4. Run it as a service (auto-restart, survives reboot)

```bash
sudo cp valoskin.service /etc/systemd/system/valoskin.service
sudo systemctl daemon-reload
sudo systemctl enable --now valoskin
```

The unit assumes user `ubuntu` and path `/home/ubuntu/Valora` — edit
`valoskin.service` first if yours differ.

**Check it:**

```bash
systemctl status valoskin
journalctl -u valoskin -f     # live logs; Ctrl+C to stop watching
```

## 5. Updating later

```bash
cd ~/Valora
git pull
.venv/bin/pip install -r requirements.txt   # only if deps changed
sudo systemctl restart valoskin
```

## 6. Back up the data (important)

The `*.json` files hold teammates' session cookies, trivia scores, follows, and
rank links. They are gitignored, so they only exist on the VM. Back them up:

```bash
tar czf ~/valoskin-backup-$(date +%F).tgz ~/Valora/*.json
```

Copy that off the box periodically (`scp` from your PC). Losing the VM without a
backup means everyone re-links and scores reset.

## Notes

- **One instance only.** The JSON storage isn't safe to run in parallel — don't
  start it twice.
- **First run after global sync:** if commands don't show in Discord, wait for
  global sync or set `DEV_GUILD_ID`.
- **Rank roles / esports jobs** run on their own schedules once the process is
  up; nothing extra to configure.
