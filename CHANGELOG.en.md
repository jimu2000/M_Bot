# Changelog

> [中文版](CHANGELOG.md)

Versioning: semantic versioning (major.minor.patch).
- New feature: minor +1 (e.g. 1.0.0 → 1.1.0)
- Bug fix / small change: patch +1 (e.g. 1.0.0 → 1.0.1)
- Major refactor / breaking change: major +1

## [Unreleased]

## [1.16.0] - 2026-11-24

### Features
- Monthly traffic (menu 19) auto-installs its dependencies (user request, 2026-11-24: every VPS missing vnstat / python3 must get them installed by default): when a host has no vnstat (only when stderr contains "vnstat ... not found"; other failures do not trigger it), the bot SSHes in and runs `apt-get update && apt-get install -y vnstat python3 curl && systemctl enable --now vnstat` (non-interactive; no-op when all three are already present), then retries the query once; a freshly installed host is annotated "vnstat 刚自动安装，数据从现在开始统计" under its traffic line (a fresh vnstat has no history yet); on install failure it reports "vnstat 未安装，自动安装失败" (retry later if apt is busy or the network is down) without affecting other hosts. Debian/Ubuntu (apt-get) only; install timeout 300 seconds

## [1.15.0] - 2026-11-24

### Fixed
- Account-name sweep (user feedback, 2026-11-24: the health-check message showed raw config keys DEFAULT / ACCOUNT2 / ACCOUNT3, with a request to check every other place too): audited all user-visible account names and replaced raw OCI config keys with the home-region Chinese name everywhere — bot: menu 15 health check (multi-region accounts keep the region-count suffix), /me (account list and per-account RUNNING counts), the menu 2 instance-list title (same display name as the top row, with the working region appended when it is not the home region), the menu 3 paused-boot list title, the daily report's quota title, the A1 health-check title, and the "current account" wording when refusing to delete the API user in use; web: the quota page's account name and the overview cloud-report account names (backend adds a profileLabel field, frontend prefers it and falls back to the config key for stale caches). The profile-upload flows (/uploadapi and /oci) still show the raw key, correctly — that is the key the user is adding

## [1.14.0] - 2026-11-24

### Features
- Multi-region second-level menu on the web panel (user feedback, 2026-11-24: the web panel had no region submenu for multi-region subscriptions, while the bot got one in v1.7.3): the five pages with an account dropdown (instances / boot volumes / quota / create / GFW) gain a region dropdown next to it — shown only for accounts with multiple subscribed regions (hidden for single-region accounts, same as the bot); switching regions reloads the page's data for the new region, switching accounts resets the region to the new account's home region; the choice is stored in localStorage and shared across pages
- Region plumbing on the backend: GET /api/profiles now returns a regions list per account (all subscribed regions, home first, with Chinese labels and isHome flags; home region only when the subscription list is unavailable); resolve_profile / get_oci_client take an optional region (home region when omitted, validated against the account's subscribed regions when given — 400 otherwise; fail-open when the subscription list is unavailable, same as the bot); every endpoint taking ?profile= now also takes ?region= (15 instance, 6 volume, 7 backup, 4 quota, 4 email, 6 object-storage endpoints, plus the GFW instances list); the create body gains a region field, and the create-defaults endpoint accepts ?region= (per-(profile, region) deployment config, so switching to Seoul auto-fills Seoul's subnet); the instance-list server cache key and the frontend cache keys gain a region dimension

## [1.13.0] - 2026-11-24

### Features
- Auto-fill network fields on the create form (user feedback, 2026-11-24: nobody knows how to fill in the availability domain / subnet — they should be selected automatically): new GET /api/create/defaults endpoint returns the availability domain and subnet for the selected account's region from the deployment config (same get_deploy_defaults as the bot's boot form: RBOT_PLAN_* env vars are the home-region defaults, region_plans.json wins when that region is configured). The form fills them on page load and when switching accounts; they stay editable, and empty when unconfigured — also left empty when the selected account's region differs from the region the deployment defaults belong to, so a home-region subnet is never filled into another account and fails the launch
- SSH key file is now optional (user feedback, 2026-11-24: fill it in when a key has been uploaded, leave it blank when there is none): the path is prefilled when the key file exists on the panel server, left blank otherwise; POST /api/create no longer requires ssh_keys_file — when blank the grabber omits --ssh-authorized-keys-file and the instance relies on root password login (on by default)

## [1.12.0] - 2026-11-24

### Features
- Auto-generate Bark encryption key (user request, 2026-11-24): menu 43 and the panel "Bark notifications" page gain a "🎲 Auto-generate Key" button that creates a random 32-char key (16 random bytes, hex-encoded — equivalent to `openssl rand -hex 16`; note `openssl rand -hex 32` produces 64 chars, too long to use as a key). A confirmation page comes first (the old key stops working immediately after generating); the new key is shown only once, with a QR code the Bark app can scan to add it (or copy it manually). The bot sends the QR as a photo, the web page shows it in the dialog; without segno installed there is no QR code, just the key text

## [1.11.0] - 2026-11-24

### Features
- Default root password for new instances is now a UUID (user request, 2026-11-24): the grabber generates a UUID4 32-hex-char password by default; the confirmation page shows it (the bot does not remember it — the message says so), and "Start grabbing" reuses the same one. Web panel create page in sync: root login toggle on by default, password field prefilled with a random UUID (regenerable), shown once in a dialog after submit
- Boot success notification reformatted after R-Bot's success message (user screenshot feedback, 2026-11-24): title "🎉 恭喜，开机成功！", fields in the order account / CPU type / CPU count / memory / disk / root password / SSH key / launch count (i/N) / elapsed time / grab attempts / region (region code + Chinese name) / IP, with a "注意📢：root 密码机器人不会记忆" note at the end; web-panel launches now also send dual-channel notifications (Telegram + Bark, Telegram only if Bark is not configured), with an extra pinned alert for paid shapes
- Bark settings completed (user feedback, 2026-11-24: Bark settings were nowhere to be found in bot or web): bot gains a top-level /bark command jumping straight to the settings page (menu 43 still exists); panel gains a "Bark notifications" page (address + 32-char key + test push + switch), sharing tgbot/data/bark.json with the bot; command menu grows from 20 to 21 commands
- Account list shows the actual subscribed-region count (user feedback, 2026-11-24): the switch-account list uses the "新加坡、3 个区" format, with the count computed from real region subscriptions; single-region accounts show no suffix

### Fixes
- Grabber dedup mistook a terminated instance for a success (real bug, found by a live test in Seoul, 2026-11-24): a same-name TERMINATED instance inside the 15-minute window was returned as "previously created successfully"; only RUNNING / PROVISIONING / STARTING instances are now accepted
- Switching account/region now returns straight to the main menu (user feedback, 2026-11-24): on success the current message is edited back into the /oracle main menu (with a confirmation line), and the intermediate account/region list messages are deleted instead of piling up; duplicate clicks within 5 seconds are deduplicated, so no more double confirmations
- Deleted boot volumes are fully hidden (user request, 2026-11-24, supersedes the v1.8.1 group/collapse approach): bot menu 7 and the web Volumes page both filter out TERMINATED / TERMINATING volumes; the deleted-volumes section is removed
- Branding sweep (user screenshot feedback, 2026-11-24): login page title and logo letter R changed to M_Bot / M, top-bar logo letter R changed to M, /start welcome text "我是 R-Bot 开源克隆机器人" changed to M-探长; "R-Bot" wording in code comments and the JS internal namespace (window.RBot) is implementation detail invisible to users, kept as-is
- README disclaimer updated to the user-supplied wording (bilingual, adding "R-Bot is its author's product, and a relatively mature one" and "no shared code"); new CHANGELOG.en.md English changelog, updated bilingually from now on

## [1.10.0] - 2026-11-24

### Features
- GFW auto IP change (menu 16, user request, 2026-11-24): menu 16 is now "GFW自动换IP" instead of a placeholder. Every 30 minutes, enabled instances' public IPs are pinged from mainland China probes (RIPE Atlas, mixed China Telecom/Unicom/Mobile); if most probes fail, the IP is judged blocked, automatically changed, and re-checked until one works; every change sends a dual-channel notification (Telegram + Bark, Telegram only if Bark is not configured — Bark is optional); at most 5 changes per cycle, then it pauses until the next cycle; with fewer than 3 reporting probes the result is "unknown" and no change is made
- Detection uses the official RIPE Atlas REST API (new module tgbot/gfw.py): itdog has forced click-captcha on its task endpoint since 2026-10, and 17CE/BOCE need paid credits; a free Atlas account can claim 50,000 credits, and one check costs only tens of credits; the API key is set in menu 16 (stored locally, 0600, never echoed), see README for the steps; web panel in sync: new "GFW IP change" page (key setup + per-instance toggles + last check + check now), sharing tgbot/data/gfw.json with the bot

## [1.9.0] - 2026-11-24

### Features
- Backup multi-select delete (user request, 2026-11-24): the bot backup list gains a "multi-select delete" mode (tick ☑️/☐ one by one, then "Delete selected" with a second confirmation; one failure doesn't affect the others); the web Volumes page backup table gains a checkbox column + select-all + "Delete selected" button (new endpoint POST /api/boot-backups/delete-batch, deletes one by one and returns a success/failure list)
- Paid-account toggle (user request, 2026-11-24): each account has a "paid" flag (default free), switchable from the bot backup list and the web quota area, sharing tgbot/data/paid_profiles.json; paid accounts have unlimited backups (quota endpoint returns paid=true, limit=null, full=false)
- Free accounts auto-delete the oldest backup when full (user request, 2026-11-24: no confirmation needed): bot and web backup creation no longer pop a confirmation; the server deletes the oldest backup and creates the new one directly (with LimitExceeded retry), and the result message says which one was deleted; the original deloldestbb confirmation branch is kept (stale buttons from old messages still work)
- Paid-shape instance pinned alert + Bark (user request, 2026-11-24, must-do): after a successful boot, if the shape is outside the free tier (anything other than A1.Flex / E2.1.Micro), an extra pinned alert is sent (Telegram message pin, verified in a 1:1 chat) + a Bark push; the daily billing check also pins an alert when it finds a paid instance (stopped / stop failed / running on the protected list); pure cost changes are not pinned
- Removed 13. profile管理 from the /oracle menu (user request, 2026-11-24: the top row already has a "Switch account" button, menu item 13 was a duplicate entry); the other numbers stay unchanged (no renumbering), and account switching itself is untouched (o:profiles callback + top-row button + region submenu all remain); menu total 43 → 42

## [1.8.1] - 2026-11-24

### Fixes
- Disk list cleanup (user feedback, 2026-11-24: a screenshot showed 11 deleted old test disks drowning the 3 live ones, with text and buttons duplicating each other): bot menu 7 now groups by state (🟢 available first, ⚫ deleted in a separate group at the end, group-header buttons only acknowledge when tapped), text keeps only the title + per-group counts (capacity shown after the available group, no longer mixed with the total), buttons are the only list (same pattern as the v1.7.0 instance list rework); orphan-disk markers moved onto the buttons (🗑️)
- Deleted boot volume detail pages no longer offer resize / backup / delete buttons (those operations are invalid on tombstone records); resize / backup / delete callbacks gain a state guard, and tapping a stale button says "该启动盘已删除，无法操作"
- Web Volumes page in sync: the main table only shows non-deleted disks (AVAILABLE first), deleted disks go into a collapsed-by-default section (expand to view), deleted rows show "已删除" in the actions column with no buttons; the section auto-hides while loading or on load failure, preventing stale account data from flashing during switches

## [1.8.0] - 2026-11-24

### Features
- Bark notifications (new module tgbot/bark.py + menu 43 "Bark 通知设置"): AES-256-CBC encrypted push (same spec as send_bark.py, cryptography encryption with an openssl CLI fallback, byte-identical); set the Bark address, 32-char encryption key, test push (independent of the switch), and switch in the menu; config stored in tgbot/data/bark.json (0600), address shown masked, key never echoed; RBOT_BARK_URL / RBOT_BARK_KEY env vars can serve as defaults
- Boot success dual-channel push (Telegram + Bark): sent automatically after a successful grab, formatted after R-Bot's success message (account, region, CPU type/count, memory, disk, grab attempts, elapsed time, public IP; password line only when a root password was set); Telegram is called directly over HTTP (works from daemon threads); public IP lookup retries 3 times, then says "获取中"; notification failure is only logged and never breaks the grab task
- Menu 19 traffic now uses vnstat (user request, 2026-11-24): no longer queries the OCI usage API (egress was always 0.00 GB, which is wrong); instead uses the user's own vnstat numbers — per-instance IP + Chinese city name (ip-api.com) + monthly ingress/egress GiB, with a total at the end; unreachable instances are listed separately with the reason; menu renamed to "19. 本月流量"
- Daily billing alert job (check_billing.py logic, moved to the OCI CLI): every morning at 08:00 Beijing time, checks all three accounts' month-to-date cost vs the SGD 1 budget; paid RUNNING instances are auto-STOPPED (stop only, never delete; work servers on the protected list are never stopped); free-tier overage alerts (A1 > 4 OCPU/24GB, E2 > 2 instances); dual-channel notification only on change (cost increase > 0.01, paid instance stopped, overage, check failure), silent otherwise; already-over-budget with no new charges is not nagged about daily (known historical bills don't alert every day)

## [1.7.3] - 2026-11-24

### Features
- One-click boot at the top of the menu 1 form (default AMD): E2.1.Micro + Ubuntu + 1 instance + image default disk (47GB) + SSH key + root login on + public IP; goes to the confirmation page (same pattern as the old menu 1 preset one-click, doesn't overwrite the form being filled; works even when the form has expired)
- Shorter instance names (user feedback, 2026-11-24): AMD free instances `AMD_YYYY-MM-DD`, ARM instances `ARM_YYYY-MM-DD` (Beijing date); other shapes keep the old format; auto-generated names on the panel create page follow the same rule when left blank
- Root login on by default in the create form (user requirement: a new instance must always allow root login); one-click AMD also has root on by default
- Region submenu (user feedback, 2026-11-24): tapping a multi-region account in menu 13 first shows a region submenu (reads real subscribed regions, Home marked "(Home)", current region marked ✅); tapping a region switches the working region; the /oracle menu top row and switch menu show the current region (e.g. "新加坡 · 首尔"); switching accounts resets the region to the new account's home region
- Region launch config (region_plans.json, keyed by profile + region): after switching the working region, launches use that region's availability domain / subnet / image; unconfigured regions fall back to the home-region env defaults; subnets are isolated per tenancy, so different accounts in the same region never share one

### Fixes
- Grabber dedup only treats a same-name instance created within 15 minutes as "previously created but response lost" (relaunching with a date name on the same day no longer mistakes an old instance for a new one); when the creation time can't be read, the old behavior applies
- Display-name dedup before launch: relaunching the same type on the same day auto-appends -2, -3 suffixes (whole batch checked); dedup failure never blocks the launch
- Instance name dates use Beijing time (UTC converted to Beijing before taking the date, avoiding off-by-one dates in the early hours)

## [1.7.2] - 2026-11-24

### Features
- Traffic guard (menu 37) rebuilt after the user's own auto_limit.sh (user screenshot feedback, 2026-11-24)
  - Three actions (cycle with a button): notify only / block network (iptables kill-switch, cuts outbound traffic, SSH escape hatch kept) / limit to 200K (tc htb throttled to 1600kbit ≈ 200KB/s); the old "auto shutdown" action is removed (a stored shutdown falls back to notify-only when read)
  - Lower thresholds: presets 10GB / 20GB / 200GB / 9TB (9216GB cap), default 200GB (matching the script's LIMIT_GB); whole-TB thresholds display as TB (e.g. 9TB)
  - Monthly traffic now uses vnstat calendar-month stats (queried over SSH per instance and summed, replacing the OCI usage API that always showed 0.00GB); the menu notes how many instances are reachable and how many are not
  - New "🔓 解除限制" button: removes the block chain and rate-limit rules on running instances (can also clear limits triggered by auto_limit.sh on the host)
  - On trigger, the action runs on all running instances with per-instance results; without a configured SSH key only notify-only is available (the menu and trigger both say so explicitly)
  - The guard uses a dedicated SSH key (env var RBOT_GUARD_SSH_KEY, generated at deploy time with its public key added to each instance)

## [1.7.1] - 2026-11-24

### Features
- Menu 20 free quota becomes three-level navigation: first an account list (tap an account for details); accounts subscribed to multiple regions show a region submenu (home region marked "(Home)"), tapping a region shows that account's quota in that region (title like `[新加坡 · 首尔]`, OCI client queries per region); single-region accounts show details directly with no submenu (user screenshot feedback, 2026-11-24)
- /oracle menu top row gains a switch-account button (`🔄 切换账号：新加坡（3 个区）`, opens menu 13), so switching no longer requires scrolling to menu 13; menu display names read from cache without calling OCI, and the bot warms the subscribed-region cache in the background at startup (user screenshot feedback, 2026-11-24)
- Panel account dropdown and bot switch menu unified: home-region Chinese name, multi-region accounts suffixed with the region count (e.g. `新加坡（3 个区）`); `/api/profiles` gains `regionCount` and `displayName` (region count from `iam region-subscription list`, cached 1 hour)
- Menu 18 subscription info: shows every account (previously only the current one), one section per account (home-region Chinese name + tenancy name + home region + description); a single account's query failure shows as "查询失败" without affecting the others (user screenshot feedback, 2026-11-24)
- Region Chinese names add Seoul (ap-seoul-1), bot and panel in sync

### Fixes
- Menu 8 cloud account user list: text keeps only the title + count, buttons are the only list (`👤 username`, non-ACTIVE users suffixed with a Chinese status word), email is shown on the detail page; previously the text list and button list appeared together with emails wrapping awkwardly (user screenshot feedback, 2026-11-24, same rework as the instance list)
- Menu 13 account switching: accounts shown by home-region Chinese name (新加坡 / 圣何塞 / 芝加哥), multi-region accounts suffixed with the region count (e.g. `新加坡（3 个区）`); the count comes from `iam region-subscription list`, cached 1 hour, and on query failure only the home-region name is shown; the switch confirmation message also uses the display name
- Operation-flow message cleanup (user screenshot feedback, 2026-11-24: after a rename, only the result message should remain): text-input prompts are deleted after the user replies (a new prompt deletes the old one first, so repeated taps no longer pile up); confirmation boxes are deleted after the operation, leaving only the result message; covers rename, reinstall, user description/email change, user deletion, restore from backup, SSL domain add, and email sender — eight flows

## [1.7.0] - 2026-11-24

### Features
- Boot volume backup & restore (M_Bot exclusive, R-Bot doesn't have it): panel boot volumes page + bot menu 7, both sides
  - Create FULL backups, default name `<volume-name>-backup-YYYYMMDD-HHMM` (Beijing time)
  - Free quota check (limit 5 per region): when full, shows the oldest backup's info and offers one-tap delete-oldest-and-continue
  - Restore a new instance from a backup (original untouched): two steps (create a boot volume from the backup, then launch with the new volume); shape defaults to the source instance's (A1 1 OCPU/6GB when unknown), with A1/E2 and boot volume quota pre-checks before restore
  - Backup list only shows non-deleted backups (TERMINATED / TERMINATING hidden, consistent with the quota count)
- Default boot volume for new instances 50GB → 47GB (matches the OCI image default; API default, create page input, and the three preset cards changed together)
- Bot instance list grouped by state (running → stopped → terminated → other, empty groups hidden; terminated instances are not hidden, just grouped separately); buttons are the only list, each showing `name · A1/E2 · public IP` (state word when there's no IP), text keeps only the title + per-group counts instead of repeating every instance (user feedback: the text list and button list used to appear together, twice)

### Fixes
- Deleting the oldest backup then immediately creating one hit OCI's quota sync delay (LimitExceeded): creation now auto-retries (up to 5 times, 15 seconds apart; verified live in Chicago, 2026-11-24)
- Volumes page account switch: clears the list and shows "加载中…" first (including the quota title, no more flashing the old account's numbers), with a load-generation guard so a slow old-generation async result no longer overwrites the new data on fast switches (found by a live browser test, 2026-11-24: the first version only cleared the list while the quota title still flashed old numbers)
- Backup name timestamps changed from UTC to Beijing time, matching the creation time shown in the panel (live browser test, 2026-11-24)

## [1.6.0] - 2026-11-24

### Features
- Instances page redesigned as cards (after R-Bot's instance management page): each card shows name + status badge, public IP, shape, specs, boot volume (size · VPU + status badge + attached badge), creation time; button matrix: SSH, power off/on/restart, serial console, rename, resize (A1 only), repair, reset image, IP management, boot volume, delete, delete (keep boot volume)
- IP management dialog: change IP / attach IPv6 / delete IPv6 in one place
- Boot volume dialog: size / VPU / state / attachment state / OCID, with online resize (grow only)
- Delete (keep boot volume): terminates the instance but keeps the boot volume, deletable later from the volumes page; the confirmation states it occupies the 200GB free quota

### Fixes
- Switching profiles now clears the list before loading, fixing the one-frame flash of old data from v1.3.0

## [1.5.0] - 2026-11-24

### Features
- Telegram command menu: the bot registers 20 commands in Telegram's menu at startup (/start /help /oracle /instances /domain /azure /aws /gcp /do /vf /solusvm /oci /command /pubkey /uploadapi /oproxy /clearlock /raninfo /me /admin), following R-Bot's assistant menu wording
- /me: my info (Telegram ID, username, panel version, RUNNING instance count per account)
- /admin: admin menu (panel status, restart panel/bot services, view version; restart needs a second confirmation)
- /oci: upload an Oracle or Azure config file (appended as a profile after validation; the private key never goes through Telegram — scp it yourself)
- /clearlock: clear a panel login-failure lock (restarts the panel service; lock state lives in the panel process memory)
- /raninfo: reset the panel admin password to a random one (pbkdf2-SHA256 hash written to env, plaintext to a 600 password file, panel restarted; the new password is sent privately once)
- Six cloud commands (/azure /aws /gcp /do /vf /solusvm): reply with setup instructions when unconfigured, and say plainly the feature isn't implemented when configured but the module is a stub — never fabricate data

## [1.4.0] - 2026-11-24

### Features
- Instance operations completed (aligned with R-Bot's web panel): detail dialog (public/private IP, IPv6 address, boot volume size), change IP, attach/delete IPv6, A1 resize, one-click repair
- Change IP: deletes the old ephemeral IP before attaching a new reserved IP (OCI refuses to attach a second public IP to a VNIC that already has one), requires confirm=true
- Resize: A1.Flex only, 1–4 OCPU / 1–24GB free-tier validation; changing the shape lets OCI reboot automatically (an explicit shutdown once hung for 17 minutes)
- One-click repair: checks instance state / primary VNIC / public IP, auto-attaches an ephemeral IP when missing, and reports all three checks
- The three IPv6 functions moved from the Telegram handlers into oci/network.py, shared by panel and bot

### Fixes
- "Has a public IP?" for IP change now uses the VNIC's public-ip field: the AD-scoped public IP list can't see REGION-scoped reserved IPs and would wrongly report no IP

## [1.3.0] - 2026-11-24

### Performance
- Instances page: per-instance public IPs fetched in parallel + 30-second server cache + 15-minute frontend cache; cold start ~5s (was 10–20s), cache hits are millisecond-level
- Cloud report: three accounts summarized in parallel + two-layer caching; cold start ~7s (was 30–60s+), overview page gains a refresh button

### Features
- New GET /api/profiles: account list (name + region + Chinese region name)
- Instances / boot volumes / quota / create pages gain a dynamic account dropdown ("name · region Chinese name"); switching reloads with ?profile=, localStorage remembers the last choice; no standalone region selector (region is an attribute of the account, same model as R-Bot)
- Create page gains a custom image OCID input: non-Singapore accounts can fill in that region's image

### Fixes
- Instances page terminate / rename / reinstall / console / batch operations previously didn't carry ?profile=, so under a non-default account they'd act on the default account; all now carry the current account (boot volumes page too)

## [1.2.2] - 2026-11-24

### Fixes
- Create page A1 spec limits changed to the free tier: max 4 OCPU / 24GB per instance (previously showed the hardware limits of 80 OCPU / 512GB, easy to launch a paid shape by mistake); frontend and backend both validate, over-limit returns 400
- Telegram menu 1 form in sync: OCPU options 1/2/4, memory options 1/6/12/24, custom values validated against the same 4 OCPU / 24GB limits
- Create page image hint rewritten in plain language (no longer mentions the internal web/config.py filename)

## [1.2.1] - 2026-11-24

### Changes
- Web panel branding unified to M_Bot: all page titles, top-bar logo, login page, and the Telegram verification SMS signature changed from the R-Bot open-source clone wording to M_Bot Oracle开源管理面板

## [1.2.0] - 2026-11-24

### Features
- Menu 1 (boot) becomes an R-Bot-style button-matrix create form: CPU type / OS / OCPU / memory / disk / count / delay, each row tap-to-select (tap again to clear); picking two delays gives a random interval
- Batch creation: up to 4 at once, quota pre-check computed as count × spec; root login with a random password (shown once in the launch message); public IP / IPv6 toggles; startup script via file upload or pasted text
- Forms can be saved as quick-boot presets (menu 17 one-click boot); CentOS maps to Oracle Linux (requires RBOT_IMAGE_OL_* images)
- Fixed an old bug: the old preset flow didn't pass OCPU/memory to the Grabber, so A1 presets were all created with default specs

## [1.0.1] - 2026-11-24

### Changes
- Project renamed: M-探长 → **M_Bot Oracle开源管理面板** (repo jimu2000/m-tanzhang → jimu2000/M_Bot, old URL redirects; the Telegram bot is still called M-探长)

## [1.0.0] - 2026-11-24

First complete release. Telegram bot /oracle menu with 42 items, web panel + web SSH, 1318 unit tests passing, verified on a live deployment.

### Features
- OCI instance full lifecycle: create (grabber), list, details, start, stop, restart, terminate, resize, rename, reinstall, serial console
- IP management: reserved IP create/delete, instance IP change, IPv6 create/delete/list
- Disk management: boot volume list/resize, VPU change, orphan volume cleanup
- Cloud account management: user details, change description, change email, API key list/delete, delete user (own-account protection)
- Batch operations: batch start/stop/restart/delete, A1 config health check
- Monitoring: instance monitoring notifications, monitoring autostart, traffic guard (notify-only / auto shutdown), stopped-instance keep-alive, domain SSL expiry alerts, daily report
- Client tools: remote shell (/command), SSH key management (/pubkey), proxy management (/oproxy), upload API config (/uploadapi)
- Grabber plans, object storage, Email Delivery, certificate management, inspection plans, cloud report
- Cloudflare DNS management (web panel + /domain)
- Web panel: login lockout, session management, optional image captcha, Telegram code login, SFTP file panel (chunked upload with progress), batch commands, terminal favorites
- MCP server (6 tools, JSON-RPC over stdio)
- Memory usage/cleanup (anti-idle reclamation), one-click liveness check, quick-boot presets

### Security
- All configuration via environment variables, no real credentials in source
- Write operations need a second confirmation; terminating an instance permanently deletes its boot volume by default
- Dangerous-command blocklist (rm -rf /, mkfs, dd, etc.)
- Login failure lockout (from 5 failures, doubling duration, 30-minute cap)
