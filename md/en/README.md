# M_Bot Oracle Cloud Open-Source Management Panel

> **Current version: v2.25.4** · See [CHANGELOG.en.md](../../CHANGELOG.en.md) for changes

[简体中文](../../README.md) | English

## Contents

- [Quick start](#quick-start)
- [🚀 One-command install](#one-command-install)
- [Getting your Telegram bot token (step by step)](#getting-your-telegram-bot-token-step-by-step)
- [Getting your OCI API key (step by step)](#getting-your-oci-api-key-step-by-step)
- [Screenshots](#screenshots)
- [Features](#features)
- [GFW auto IP change (menu 16, new in v1.10.0)](#gfw-auto-ip-change-menu-16-new-in-v1100)
- [Manual install](#manual-install)
- [Configuration rules](#configuration-rules)
- [Tests](#tests)
- [Deliberately not implemented](#deliberately-not-implemented)
- [License](#license)

---


M_Bot is an open-source Oracle Cloud management panel written in Python:
Telegram bot + web panel + web SSH + instance grabber + Cloudflare DNS + MCP
server. All code here is independently written, fully visible and auditable.

> ⚠️ Disclaimer: This project is an independent open-source project, not
> affiliated with any similar closed-source tool in any way, with no
> shared code.

**Highlights:**

- **Secure and independent**: independently written code, 3421 unit tests; the OCI private key only signs requests locally and is never sent out; every free feature is open, no paywall.
- **Multi-client**: Telegram bot + FastAPI web panel + MCP server, with shared config and notifications (Telegram + encrypted Bark push).
- **Low-maintenance**: terminating an instance deletes its boot volume by default (no surprise charges); RIPE Atlas probes auto-swap a firewalled IP; Let's Encrypt domain and IP certificates are issued and renewed automatically.

Use case: run one Oracle Cloud Always Free server and manage instances, grab capacity, swap IPs and do DNS from a bot or a web page, without opening the console.

## Quick start

Prerequisites (all required):
1. A Debian / Ubuntu server (root access, 1 vCPU / 1 GB RAM is enough).
2. A Telegram bot token: see "Getting your Telegram bot token (step by step)" below.
3. Your Telegram user ID: send `/start` to @userinfobot (digits only).
4. An OCI API key: see "Getting your OCI API key (step by step)" below.

## 🚀 One-command install

Once the prerequisites are ready, install with one command (run as root; first run installs, later runs update and restart):

```bash
wget -O install.sh https://raw.githubusercontent.com/jimu2000/M_Bot/main/install.sh && chmod +x install.sh && bash install.sh
```

The installer asks for your token, user ID, panel password (leave empty to
auto-generate a 16-character random password, shown once), and domain
(leave empty to get an IP certificate automatically). After installing:

- With a domain: open `https://your-domain/login` (Let's Encrypt
  certificate, requested automatically). Pick a validation method during
  install: `http` (port-80 validation, default) or `cfdns` (Cloudflare DNS
  validation; needs a CF API token with Zone:DNS:Edit + Zone:Zone:Read;
  validation uses no ports, and the panel moves to port 9527 if 443 is
  already taken by another service).
- Without a domain: a Let's Encrypt IP certificate is requested for the
  server's public IP automatically (shortlived, ~6 days validity, renewed
  by cron every 2 days); open `https://server-IP:9527/login` (a real
  certificate, no browser warning; falls back to self-signed if issuance
  fails — click through the browser warning then; IP validation needs
  port 80 open in the cloud security list).
- Username `admin`; the password is the one you entered or the generated one
  (also saved in `/root/.mbot-admin-password`).

Manage it with: `bash install.sh start|stop|restart|status|log|upgrade|uninstall`.

The detailed steps for the token and the OCI key follow below; import them after installing.

## Getting your Telegram bot token (step by step)

The token is the bot's credential for the Telegram Bot API. It looks like
`123456789:AAH...` (digits, a colon, then letters and digits, about 35
characters). The digits before the colon are the bot's ID — you don't enter
it separately; paste the whole token when installing.

**New bot (if you don't have one yet):**

1. Open Telegram, search for @BotFather, press Start.
2. Send `/newbot`.
3. Pick a display name (e.g. M-探长).
4. Pick a username ending in `bot` (e.g. `vega_oci_bot`); it must be unique.
5. BotFather replies with your token — copy the whole string.

**Existing bot (to retrieve the token):**

1. Open @BotFather, send `/token`.
2. Pick your bot.
3. BotFather sends you the token — copy the whole string.

**Your Telegram user ID (for the allowlist — don't confuse it with the token):**

Send `/start` to @userinfobot; it replies with your numeric ID. Enter that as
the user ID during install; only that ID can operate the bot.

**If your token leaks:**

In @BotFather: `/mybots` → pick your bot → API Token → Revoke to generate a
new one; the old one stops working immediately.

**Note:** anyone with the token can control your bot — never post it in a
group chat or anywhere public.

**Note:** run only one bot per token, on one machine. Two machines sharing
a token compete for updates, so commands land on a random one — and fail
when that machine is missing the config. For a second test machine, create
another bot and use its own token.

## Getting your OCI API key (step by step)

The OCI API key is two things, sent through two different channels — don't
mix them up:

- **The config text** (with the five keys tenancy / user / fingerprint /
  region / key_file): send it to **your own bot** (a private chat — not a
  group, not any person). The bot runs on your own server, so the config
  only ever lands on your own machine.
- **The private key file** (.pem): **never send it through Telegram**;
  copy it to the server with scp.

**Step 1: Generate the key pair (on your computer or server):**

```bash
openssl genrsa -out oci_api_key.pem 2048
chmod 600 oci_api_key.pem
openssl rsa -pubout -in oci_api_key.pem -out oci_api_key_public.pem
openssl rsa -pubout -outform DER -in oci_api_key.pem | openssl md5 -c
```

The last command prints the fingerprint, like `aa:bb:cc:...` — save it,
you'll need it in step 3.

**Step 2: Add the public key in the OCI Console:**

1. Log in to the OCI Console (cloud.oracle.com).
2. Top-right avatar → My Profile → API Keys → Add API Key.
3. Paste the contents of the **public** key file (`oci_api_key_public.pem`,
   the one starting with `-----BEGIN PUBLIC KEY-----`), click Add.
4. The console shows a fingerprint — check it matches the one from step 1.

**Step 3: Get the tenancy OCID and user OCID:**

- User OCID: on the My Profile page, looks like `ocid1.user.oc1...`.
- Tenancy OCID: avatar menu → tenancy name → OCID, looks like
  `ocid1.tenancy.oc1...`.

**Step 4: Write the config text:**

```ini
[DEFAULT]
user=ocid1.user.oc1..xxxx
fingerprint=aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99
key_file=/root/oci_api_key.pem
tenancy=ocid1.tenancy.oc1..xxxx
region=ap-singapore-1
```

Set region to your home region (Singapore `ap-singapore-1`, San Jose
`us-sanjose-1`, Chicago `us-chicago-1`). For more accounts, add more
sections (`[ACCOUNT2]`, `[ACCOUNT3]`, ...); they all import in one go.
R-Bot style `oci=begin` / `oci=end` blocks work too.

Set key_file to the private key's actual path on the server: put the key
directly under `/root/` and write `/root/oci_api_key.pem`; a key generated
with `oci setup config` lives at `~/.oci/oci_api_key.pem` (i.e.
`/root/.oci/oci_api_key.pem`) — write that path, no need to move it. Any
path under `/root/` is kept as-is.

**Step 5: Send the config to the bot, scp the private key to the server:**

- Config: open a private chat with your own bot, send `/uploadapi`, then
  send the config file (.txt / .conf) or paste the text, confirm, and the
  bot writes it to `~/oci_config` on the server (i.e. `/root/oci_config`).
  Can't find your bot? Search Telegram for its @username (the one ending in
  `bot` you picked when creating it), or in @BotFather send `/mybots`, pick
  your bot, and tap its @username — or open `t.me/<username>` directly. Note: the bot only replies after M_Bot
  is installed with its token — before that, it won't respond. If the server
  still has an old `~/.oci/config`, ignore it — M_Bot reads `~/oci_config`
  first and only falls back to the old file when it doesn't exist.
- Private key: copy it to the server with scp (add `-P <port>` if you
  changed your SSH port):

```bash
scp oci_api_key.pem root@<server-IP>:/root/oci_api_key.pem
```

If key_file is already under `/root`, the bot keeps that path; otherwise it
rewrites it to `/root/<PROFILE>.pem` — put the file where it tells you.

**Step 6: Verify:**

Send `/oracle` to the bot; if it lists your accounts and instances, it works. The install command is in "🚀 One-command install" above.

## Screenshots

Live demo (no login, simulated data): https://demo.youu.eu.org

![Overview / cloud report](../../docs/screenshots/overview-en.png)
![Instance list](../../docs/screenshots/instances-en.png)
![Web SSH terminal](../../docs/screenshots/terminal-en.png)
![Task management and grabber plans](../../docs/screenshots/tasks-en.png)

## Features

**OCI core (`oci/`, drives the official OCI CLI, no SDK)**
- Instances: list / detail / create / start / stop / reboot / terminate
  (boot volume deleted by default, so nothing keeps billing) / rename /
  resize / reinstall / serial console
- Batch operations: batch start / stop / reboot / terminate
- Networking: change public IP, reserved IP management, IPv6 create / delete / list
- Volumes: boot volume list / resize / delete / orphan cleanup / VPU change
- Quotas: free-tier summary, A1 shape health check, pre-launch quota check
- Object Storage: bucket create / delete / object upload / download / list
- Email Delivery: sender management
- Grabber (`grabber.py`): `--no-retry` fast fail, jittered intervals,
  exponential backoff on 429, quota-exhaustion detection (stops and notifies
  instead of retrying forever), CPU model filtering (e.g. keep only AMD EPYC 7J13)

**Telegram bot (`tgbot/`, python-telegram-bot)**
- `/oracle` menu with 45 items: launch/grab, IP management, instance
  management, disk management, cloud accounts, monitoring, quotas, traffic,
  costs, batch operations, A1 health check, traffic guard, keep-alive,
  SSL expiry alerts, object storage, email senders, grabber plans,
  Oracle global region speed test, Cloudflare preferred IP…
- GFW auto IP change (menu 16): every 30 minutes, pings enabled instances'
  public IPs from mainland China probes (RIPE Atlas); if blocked, changes
  the IP and re-checks until one works; notifies on every change via
  Telegram + Bark (Bark optional — Telegram only if not configured)
- Boot generates a UUID root password by default (32 hex chars), shown on
  the confirmation page and included in the Telegram + Bark dual-channel
  success notification (web-panel launches notify the same way, and the
  password is shown once in a dialog after submit)
- Bark notification settings (menu 43 / `/bark` command / panel "Bark"
  page, all sharing one config): AES-256-CBC encrypted push, with
  address + 32-char key + test push + on/off switch; the key can be
  auto-generated (random 32-char hex) with a QR code the Bark app scans
- `/domain`: Cloudflare zone and DNS record lookup
- Client tools: `/command` remote shell (dangerous-command blocklist),
  `/pubkey` key management, `/oproxy` proxy settings, `/uploadapi` OCI config upload
- Strict allowlist: non-allowlisted users are rejected on every command;
  scheduled jobs: instance monitoring, daily report, SSL expiry checks

**Web panel (`web/`, FastAPI + WebSocket + Paramiko + xterm.js)**
- Login (pbkdf2 hashing, doubling lockout up to 30 minutes, optional image
  captcha, optional Telegram code login)
- Pages: overview / instances / accounts / create / quota / boot volumes /
  tasks / GFW IP change / Bark notifications / terminal / DNS / object
  storage / email / certificates / inspection plans / cloud report
- Accounts page: add a tenant by pasting config text or uploading a config
  file (auto-parsed, R-Bot format supported, several accounts at once);
  existing accounts are filtered out automatically, and private keys (.pem)
  are uploaded in the confirm step (contents validated as a private key,
  saved with 0600 permissions, path forced under home, never through
  Telegram); a [cloudflare] section in the config is saved too, and
  compartment env vars are added automatically; new accounts take effect
  after restarting the bot and the panel
- IAM user management (accounts page, synced with bot menu 8): list IAM
  users, add users, create a Bot API user (group and policy created
  automatically), add API keys (private key shown once), reset console
  passwords, delete users; creating a user or adding a key also generates
  a ready-to-use OCI config file (downloadable as oci_config.txt, paste it
  into "Add tenant"); deletion guards block removing the current API user
  and the last administrator
- Create instance: the availability domain and subnet auto-fill from the
  deployment config for the selected account, and the SSH key file is
  prefilled when one exists on the server (optional — root password login
  alone is enough to boot)
- Multi-region submenu: the instances / volumes / quota / create / GFW
  pages show a region dropdown next to the account dropdown (multi-region
  accounts only); switching regions reloads the page for the new region,
  switching accounts resets the region to the home region
- Web SSH: multi-tab, reconnect with snapshot replay, session suspend, idle cleanup
- SFTP file panel: list / download / chunked upload with progress / mkdir /
  delete / rename / online edit, three-layer path traversal protection
- Batch commands, terminal favorites, grabber plans (cron scheduling), OCI proxy settings

**Cloudflare DNS (`clouds/cloudflare.py`)**
- List zones, DNS record CRUD, proxy toggle, SSL certificate expiry checks

**MCP server (`mcp_server.py`, stdlib JSON-RPC over stdio, zero new dependencies)**
- 6 tools: list instances, instance detail, quota, free-tier summary,
  list zones, list DNS records

**Other clouds**: `clouds/` has an abstract interface plus stubs for
AWS / GCP / Azure / DigitalOcean / SolusVM (not implemented yet — real
verification needs accounts on those clouds).

## GFW auto IP change (menu 16, new in v1.10.0)

When a proxy server's public IP gets blocked from mainland China, clients
there can't connect. This feature watches for that:

- **Check**: every 30 minutes, pings enabled instances' public IPs from
  mainland China probes via [RIPE Atlas](https://atlas.ripe.net)
  (5 probes, 3 packets each, mixed telecom/unicom/mobile). Fewer than 3
  reporting probes means "unknown" — no action. A majority failing means
  blocked.
- **Change**: on blocked, changes the public IP (delete the old ephemeral
  IP, attach a new reserved one) and re-checks; keeps changing while the
  new IP is still blocked; at most 5 changes per cycle, then pauses until
  the next cycle.
- **Notify**: every IP change sends a dual notification (Telegram + Bark;
  Bark is optional — Telegram only if not configured). Healthy checks stay
  silent.
- **Toggle**: per-instance on/off from menu 16 in the bot or the
  "GFW 换 IP" page in the web panel (shared config).

One-time RIPE Atlas API key setup (free):

1. Register at [atlas.ripe.net](https://atlas.ripe.net) (RIPE NCC Access account);
2. Click **Claim 50,000 free credits** on the My Credits page (one check
   costs tens of credits; for long-term use, run a RIPE Atlas software
   probe on your own server to earn ~21,600 credits per day);
3. Create a key of type "create a new user-defined measurement" on the
   [Keys page](https://atlas.ripe.net/keys/);
4. Send the key to the bot (menu 16 → Set API Key) or paste it on the
   web panel's "GFW 换 IP" page. The key is stored only on the server
   (`tgbot/data/gfw.json`, 0600), never echoed or logged.

Why RIPE Atlas: itdog has forced click-captcha verification on its task
endpoints since 2026-10 (unusable for unattended monitoring), and 17CE /
BOCE require paid credits; Atlas is a free service from RIPE NCC with a
stable REST API. Note that ping failing is evidence of "suspected"
blocking, not proof, and the instance itself must answer ICMP (not
blocked by its security list), otherwise every check reports blocked.

## Manual install

Prefer the one-click install above; manual install is for those who want
to control every step:

```bash
git clone https://github.com/jimu2000/M_Bot.git
cd M_Bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install oci-cli          # OCI calls go through the official CLI
```

**1. OCI authentication** (pick one, first recommended):

```bash
# Option 1: interactive setup (recommended)
oci setup config
# Option 2: write ~/oci_config by hand (user / fingerprint / tenancy / region / key_file)
```

Option 1 creates the legacy path `~/.oci/config`, which M_Bot still reads;
the first `/uploadapi` import migrates it to `~/oci_config`.

`~/oci_config` can also hold a `[cloudflare]` section for Cloudflare
credentials (`cf_token`, or `cf_key` + `cf_email`); the bot's `/domain` and
the panel DNS page use it, and the installer reads it first for `cfdns`
certificate verification. Precedence: saved in bot/panel > OCI config
`[cloudflare]` section > environment variables.

API private key (`key_file`): the `.pem` file generated in the OCI console.
Download it to your server, e.g. `/root/1.pem` (any path works), `chmod 600`.
How to get it: OCI console → user avatar (top right) → My Profile → API Keys →
Add API Key → Generate API Key Pair → download the private key and config snippet.
Or generate locally: `openssl genrsa -out /root/1.pem 2048`, then paste the
public key into the console.

**2. Environment variables** (all deployment values come from env vars, never code):

```bash
# Required: root compartment (i.e. tenancy OCID); one per account
export MBOT_COMPARTMENT_DEFAULT="ocid1.tenancy.oc1..xxxxxxxx"
export MBOT_COMPARTMENT_ACCOUNT2="ocid1.tenancy.oc1..xxxxxxxx"   # optional
export MBOT_COMPARTMENT_ACCOUNT3="ocid1.tenancy.oc1..xxxxxxxx"   # optional
# Image OCIDs (for instance creation; differ per region, see oci compute image list)
export MBOT_IMAGE_AARCH64="ocid1.image.oc1..xxxxxxxx"
export MBOT_IMAGE_X86_64="ocid1.image.oc1..xxxxxxxx"
# Telegram bot (if you use it)
export MBOT_TELEGRAM_TOKEN="123456789:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export MBOT_ALLOWED_USERS="123456789"   # your Telegram user ID, comma-separated
# Web panel (if you use it)
export MBOT_WEB_PASSWORD_HASH="$(python3 -c "import hashlib,os;print(hashlib.pbkdf2_hmac('sha256',b'your-password',os.urandom(16),100000).hex())")"
export MBOT_WEB_SECRET="$(openssl rand -hex 32)"
# Optional: OCI CLI path, Cloudflare credentials, serial console SSH public key
export MBOT_OCI_CLI="/opt/rbot-clone/venv/bin/oci"
export CF_API_TOKEN="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export MBOT_CONSOLE_SSH_KEY_FILE="/opt/rbot-clone/web/data/console_rsa.pub"
```

**3. Run**:

```bash
# Telegram bot
python3 -m tgbot.bot
# Web panel (binds 127.0.0.1:9527 only; reach it via SSH tunnel)
uvicorn web.app:app --host 127.0.0.1 --port 9527
# MCP server (stdio, for Claude Desktop and similar clients)
python3 mcp_server.py
```

For production, run both services under systemd — see `docs/deployment.md`
(includes the atomic upgrade script `scripts/deploy.sh`).

## Configuration rules

- **Config block at the top of each file**: constants, thresholds and paths live
  in the config section at the head of every module — no digging through code.
- **Deployment values via environment variables**: compartments, image OCIDs,
  subnets, security lists, tokens and password hashes are all injected from
  env vars; the source only contains placeholders (e.g. `ocid1.tenancy.oc1..xxxxxxxx`).
- **Private keys stay local**: the OCI private key is only handed to the
  official OCI CLI for local request signing; the code never reads, prints
  or transmits it.

## Tests

```bash
python3 -m unittest discover -s tests -t .
# Ran 3421 tests ... OK
```

3421 unit tests (mostly mocked; write operations never touch production).
Real-API verification notes live in `docs/verification*.md`: read-only calls
verified against the real OCI / Cloudflare APIs; write operations (create /
change IP / resize / extend / terminate) verified for real on test instances.

## Deliberately not implemented

- A paid privileges system (payments, feature gates) — the equivalent features
  are free for everyone here
- Deleting the current API key (would kill the bot itself)
- Disabling banned accounts (irreversible; do it manually)

## License

MIT — see [LICENSE](../../LICENSE).
