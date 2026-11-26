# Changelog

> [中文版](CHANGELOG.md)

Versioning: semantic versioning (major.minor.patch).
- New feature: minor +1 (e.g. 1.0.0 → 1.1.0)
- Bug fix / small change: patch +1 (e.g. 1.0.0 → 1.0.1)
- Major refactor / breaking change: major +1

## [Unreleased]

## [2.13.1] - 2026-11-27

- Fixed IAM password-reset errors: none of the three users could be reset, for a reason — rbot-test and api_user have console login disabled (can-use-console-password=false), and the admin account is the current API user itself, which OCI does not allow resetting via API; the reset now pre-checks both cases and explains why instead of waiting for OCI to fail; OCI errors are translated into Chinese instead of dumping the raw ServiceError JSON into the toast; users whose password cannot be reset no longer show a "Reset password" button (the list endpoint returns current_api_user_id, and the frontend checks capabilities and the current API user)
- New users and new API keys now come with an OCI config file: the bot already sends a config snippet after creating a Bot user, but the web panel only returned the private key PEM; creating a Bot user and adding an API key now both return a ready-to-use config (section/user/fingerprint/key_file/tenancy/region) alongside the private key, shown as a downloadable oci_config.txt that can be pasted straight into "Add tenancy" on the accounts page

## [2.13] - 2026-11-27

- Web IAM user management (same as bot menu 8): the Cloud Accounts page gains an "IAM Users" card — list IAM users per account, add ordinary users, create the dedicated Bot API user (api_user + Group_for_Api_used + Api_User_Policy, idempotent reuse), add an API key to a user, reset a console password, and delete users; a new API key's private key and a reset temporary password are returned only once in that response, with download/copy in the UI, and are gone after refresh; delete protections match the bot (cannot delete the API user/key the current account uses, cannot delete the last member of the Administrators group)
- Fixed Cloud Accounts page layout on phones: the configured-accounts table (7 columns) and the add-tenant parse table (5 columns) had no horizontal-scroll container, so they stretched the page and clipped the outer columns; both tables are now wrapped in .table-wrap and scroll horizontally on narrow screens

## [2.12] - 2026-11-27

- Telegram bot speedup (patch integration): instance-list single-flight (concurrent requests for the same account/region/language share one load) + VNIC query concurrency capped at 2; instance-detail 12s cache + single-flight; CPU-model SSH detection single-flight (one probe per instance) + max 2 concurrent + 60s cooldown after failure; in-flight loads started before a cache clear no longer write stale state back
- IAM cloud-account management: new oci/iam.py (IAM user/API-key management, RSA key generation, one-click Bot API user creation); menu 8 can add ordinary users, create a Bot API user (api_user + Group_for_Api_used + Api_User_Policy), add an API key to a user (private key delivered once as a Telegram file), and reset a console password; deleting a user/key still blocks the API user/key the bot itself is using
- Admin protection (new): OCI itself does not stop you deleting the last admin, so M_Bot now does — before deleting a user it checks the Administrators group and refuses when the target is its only member, telling you to add another admin first; checked both on the delete prompt and at final confirmation
- Patch integration notes: the ChatGPT overlay patch (tgbot/handlers.py, tgbot/handlers_extra.py, oci/iam.py) was merged and adapted — fixed the patch's own test overwriting sys.modules['telegram'] (which broke full-suite discovery), the max_workers argument for attach_public_ips_bulk, test mocks for list_users/list_api_keys after the CLI-to-SDK switch, and the wait parameters in create_user; full suite 3223 tests pass

## [2.11] - 2026-11-27

- Multi-worker prerequisite unblocked: the 4 in-memory stores — sessions, login lockout, Telegram codes, and magic-login tokens — move together to SQLite (web/data/mbot_sessions.db, WAL mode, 0600), so login state, lockout, codes, and magic tokens are consistent across workers; public method signatures are unchanged and all 17 call sites are untouched; one-time code consumption and lockout counting use BEGIN IMMEDIATE transactions for atomicity (two workers verifying the same code at once: only one succeeds). New web/auth_store.py (AuthDB) plus 21 cross-worker tests; full suite 3216 tests pass; two-process live check: login on worker 1, then /api/auth/me on worker 2 returns 200; logout on worker 2, then worker 1 immediately returns 401; 5 wrong passwords on worker 1, then login on worker 2 returns 429
- Behavior change: login sessions now survive a restart (previously in-memory and lost on restart); they still expire by TTL (default 12 hours, "remember me" 30 days) and logout revokes immediately; workers no longer clear sessions at startup because they would delete each other's live sessions
- Enabling multi-worker: add --workers 4 to the ExecStart line of rbot-clone-web.service on .160 (the service file lives only on .160, not in the repo); 100/1000/10000-user load tests under 4 workers to be measured after deploy

## [2.10] - 2026-11-27

- Forced-refresh slimming: every forced refresh rebuilt the OCI client, and private-key parsing (~0.39s per refresh, 61%) plus TLS/CA rebuild (~0.14s, 22%) dominated CPU. The web layer now caches and reuses OCIClient per profile+region (cleared for an account when that account is deleted), and fetches the instance list and VNIC attachment list in parallel. Forced-refresh CPU drops from ~1.01s to ~0.34s (-66%), wall time from ~2.69s to ~1.8s; everyday menu taps (cache hits) stay at ~0s
- Core fix: when a grabber background thread wrote its completion log and the shared file's directory had been deleted (e.g. a test cleaning up its temp dir), the write raised FileNotFoundError, killing the thread and leaving the task stuck in "running". TaskRegistry updates (web and bot) now only log a warning on write failure instead of raising, so the thread finishes cleanly; test tearDown waits for grabber threads before deleting the temp dir. 2 regression tests added; full suite 3195 tests pass
- Multi-worker not done: session store, login lockout, Telegram codes, and magic-login tokens are 4 in-memory stores that must move to SQLite together before workers can be enabled — migrating only sessions would count lockouts per worker (a security regression) and break cross-worker code verification; deferred to the next round

## [2.09] - 2026-11-26

- Telegram bot speedup: updates are now processed concurrently (concurrent_updates(4), rapid taps no longer queue up); instance list gains a 30-second cache so repeat taps return instantly; public-IP lookup switches to the bulk API (3 SDK calls per instance down to 1+N); the quota button answers immediately so the spinner stops at once; the detail page fetches instance and IP in parallel and moves CPU-model SSH detection to the background (shows "Detecting…" first); OCIClient is cached per profile+region. Measured: list 2.8–3.2s → ~0.3s on cache hit, detail ~1.2s, quota ~1.5s
- Panel CPU burst fix: VNIC attachment list and public-IP map join the shared cache and prewarm, so a cold menu switch no longer fires 1+N SDK calls; the shared cache gains singleflight (10 concurrent cold requests collapse to 1 OCI call); background CPU-model SSH detection is throttled (max 2 concurrent handshakes + 60s cooldown); JSON model conversion allocates less. Cold menu switch (instance list) measured 2.51s / 0.81 CPU-s before this release; post-fix numbers to be measured after deploy

## [2.08] - 2026-11-26

- DD flow fix: upstream InstallNET.sh ends by printing "Input 'reboot' ..." and exiting 1 without rebooting (upstream commented out its own `sleep 5 && reboot`), so DD waited forever for the new system and hit the 30-minute timeout. The generated script now sleeps 5 seconds and reboots regardless of InstallNET.sh's exit code (on success it boots into the installer; on failure it simply reboots back into the original system). This is exactly how the sentinel test machine ARM_2026-11-26 got stuck (serial console showed it still on the original Ubuntu kernel)

## [2.07] - 2026-11-26

- Unified config file M_bot_config.conf moves to the home directory: the default location changes from the install directory to ~/M_bot_config.conf (/root/M_bot_config.conf for a root install); fresh installs write it there and the systemd MBOT_CONFIG_FILE points at it. Upgrades automatically move an existing install-directory copy to the home directory on first start (once, no symlink left behind); deployments with an explicit MBOT_CONFIG_FILE are untouched. The .env file, admin password, and oci_config stay in the install directory

## [2.06] - 2026-11-26

- Unified config file M_bot_config.conf: plain-text .conf format (styled after R-Bot's client_config.txt, with bilingual `#` comments). The three manually maintained sections — oci (multiple profiles, each with user/fingerprint/key_file/tenancy/region), cloudflare, and bark — come first, followed by a 44-`#` separator and reserved sections not used yet (username/password, local_address/model, azure, ssh, solusvm). Files generated by mbot all live in the install directory (.env, admin password, oci_config, M_bot_config.conf); /root keeps only the user's own .pem private keys (key_file is read from whatever path is written). oci_config is auto-generated from the .conf at startup and no longer hand-edited; the MBOT_* values the bot rewrites itself stay in the env file. New module mbot_config.py (parse, save, migrate, generate oci_config), plus an M_bot_config.example.conf template
- Certificate method auto-detection at install time: domain + Cloudflare credentials (CF_Key/CF_Email or CF_API_TOKEN) → cfdns; domain without credentials → http; no domain → IP certificate. An explicit MBOT_INSTALL_CERT_METHOD still wins; update mode now honors it too (it used to ignore it and default to http)
- Panel port prefers 443 at install time: if 443 is free the panel listens on 443 (the standard HTTPS port), otherwise it falls back to 9527 when something like xray already holds 443. Self-signed, IP-certificate, and domain-certificate paths all use the same preferred port; the firewall only opens the port the panel actually uses; the self-test and the printed panel URL follow the real port

## [2.05] - 2026-11-26

- New instances support a two-stage "DD reinstall + init" flow: the boot form gains a "DD reinstall" switch; when enabled, the instance is created with an InstallNET.sh script that reinstalls Debian 13 (SSH port 14422, 1024MB swap), and after the reinstall finishes the bot uploads an init script over SSH in the background and runs it (system update, BBR, SSH hardening to key-only login on port 14422, UFW, Xray, Fail2Ban, CrowdSec, traffic watchdog, etc.), with success or failure pushed over both notification channels; every new instance gets a generated UUID root password sent with the boot info. New module oci/dd_init.py (DD script generation, waiting for the reinstall, script upload and execution, timeouts and event callbacks); the bot boot form and the web create endpoint both support the dd parameter
- CPU optimization rounds 4–6: the instance list page now fetches public IPs in bulk instead of one call per instance (from 1+2N SDK calls down to 2+N); a new shared OCI data cache in the web layer (web/oci_cache.py, raw instance/boot-volume lists cached for 30 seconds keyed by profile+region+compartment, shared by the instances, quota, and volumes endpoints, with the quota summary fetching the instance list only once); a background cache pre-warming thread (refreshes cache entries about to expire every 10 seconds, warming only — it never warms entries nobody has requested, so an idle panel makes no background OCI calls). Cold-request system CPU dropped from 42% to about 13%, and after 35 seconds idle the quota endpoint returns in 0.0 seconds
- Demo mode: with MBOT_DEMO_MODE=1 the panel serves simulated data, demo/demo logs in without a real password, and write operations return a fake success without calling OCI or changing any real state
- Analytics switched from Clarity to self-hosted Matomo (MBOT_MATOMO_URL / MBOT_MATOMO_SITE_ID, the tracker loads only when both are set)
- Web frontend JS fixes (display and interaction issues on the instances, quota, volumes, and other pages)
- Boot success notifications now include the CPU model (falls back to inferring AMD/ARM from the Shape when the model is unavailable)
- New-instance root password supports a fixed password: when the MBOT_FIXED_ROOT_PASSWORD environment variable is set, all new instances use it (only one password to remember); otherwise each instance gets a generated UUID. The password lives only in the deployment server's environment file; the panel never stores, reads, or displays it

## [2.04] - 2026-11-25

- Task management: finished tasks can now be deleted (reported by the user on 2026-11-25). The Operations column shows a "Delete" button for finished tasks, which removes the task record and logs after confirmation; running tasks have no delete button and must be cancelled first (the backend also blocks deleting a running task with a 400). Added DELETE /api/tasks/{id}, TaskRegistry.delete(), and task_store.delete_record(). Task status now follows the current language (Chinese mode: DONE → 已完成, CANCELLED → 已取消, RUNNING → 运行中, FAILED → 失败, etc.; English mode shows the English original), via the new TASK_STATUS_ZH map and taskStatusLabel() in i18n.js, following the existing localization pattern for instance and volume states
- DNS management: no delete button visible (reported by the user on 2026-11-25). The delete button already existed in code; the "Content" column was too wide and pushed it off-screen. Fix: the content column gets a dns-content style (max-width 320px, wraps), so the Operations column (edit/delete) stays visible
- Instance cards showed "Not detected" for the CPU model (reported by the user on 2026-11-25). When no model is detected, the architecture is now inferred from the Shape (A1.Flex → ARM, E2 → AMD), so a known shape never shows "not detected"; a detected model is still shown as-is. Added cpuModelLabel() in instances.js, shared by the card and the detail view
- Auto-upgrade now checks nginx too (requested by the user on 2026-11-25). The nightly 04:00 auto-upgrade used to check only mbot; it now checks both mbot and nginx, and when both are already latest it only logs and skips without touching any service; only the one with a new version is upgraded. Added nginx_update_available() (apt installed vs candidate version comparison) and upgrade_nginx() (--only-upgrade, --force-confold to keep the existing config, reload after validation) in install.sh; a failed mbot check no longer returns early, the nginx check still runs

## [2.03] - 2026-11-25

- Desensitization fix (reported by the user on 2026-11-25): comments in the public install.sh (lines 546-547) contained the user's real domain; fixed by replacing the real domain with example.com in the comments (both the English and Chinese parentheticals); the public tree now contains no trace of that domain; the panel's access domains never entered the public tree; comments only, no functional code changed

## [2.02] - 2026-11-25

- Bind domain: no response after typing the subdomain (reported by the user testing on a phone on 2026-11-25). Root cause: python-telegram-bot runs only the first matching handler per group, and all three text handlers sat in the default group 0, so the rename/reinstall text handler registered first swallowed every plain-text message and the bind-domain handler never ran. Fix: text handlers are split by purpose into groups (rename/reinstall group 0, bind domain group 1, client tools group 2, /oci paste group 3), each recognizing only its own user_data state. Three extra guards were added: starting a bind flow clears a stale pending_input, and the rename/reinstall, client-tools, and /oci handlers all yield when bind_domain_pending already has a Zone selected. The same latent bug was found and fixed for /oci pasted text. After typing the subdomain there is now always an explicit response (the existing success / already-exists / conflict-confirm / invalid-format / Cloudflare-error branches are unchanged, they just finally run)
- "Message is not modified" errors: all 100 query.edit_message_text call sites now go through menu_tracker.safe_edit, which compares the current text and markup before editing and skips the API call when they are identical; if the API still reports it, it is logged at debug only — no ERROR spam and nothing sent to the user
- "Message to edit not found": safe_edit falls back to sending a new message when the message to edit is gone (deleted / no longer exists); the global error_handler no longer forwards the raw Telegram text to the user for this case either, it sends a plain-language note ("the message you tapped is gone, please send the command again to reopen the menu")
- /domain DNS list shows no bound records and nothing deletable (reported by the user testing on a phone on 2026-11-25). Bot: an A record whose content IP is a public IP of an instance managed by M_Bot is now marked "🔗 Bound to instance {name}", with a bound-count summary at the end of the list; the instance map prefers the cached instance list and falls back to one best-effort OCI fetch when the cache is empty, degrading to no annotation (never blocking the list) on failure; in the delete picker, bound records sort first with their 🔗 instance name, and the confirm page warns (bound: deleting removes the domain pointer from the instance; MX/TXT/CNAME and other non-address records: deleting may affect mail/verification); after deletion the bot confirms "Deleted {record}" and only the selected record is deleted. Web: GET /api/dns/zones/{zone_id}/records now adds a bound_instance field per record (the instance display name when the record is an A record whose IP equals an instance public IP, otherwise null; OCI failure degrades to all null without blocking the list), the frontend shows a "🔗 Bound to instance {name}" badge, and the delete confirm warns for bound records and for MX/TXT/CNAME
- Login page GitHub link dedup and rename (reported by the user testing on a phone on 2026-11-25): the "GitHub开源主页" (GitHub Home) line added in 2.01 is removed (it duplicated the footer "GitHub 项目" link, leaving two GitHub links on the login page), keeping only the footer one; the footer "GitHub 项目" is renamed "GitHub 免费开源项目" (GitHub Free Open Source Project), with the link unchanged (still https://github.com/jimu2000/M_Bot, new tab); the English copy is updated the same way, so the login page now shows the GitHub link exactly once
- /start welcome and main menu gain GitHub (user request, 2026-11-25): the "❤️ 一键测活" (Health Check) button on the /start main menu is replaced by a "GitHub 免费开源项目" URL button opening https://github.com/jimu2000/M_Bot; the health-check feature itself stays, still reachable as item 15 in the /oracle menu; the /start welcome message gains a line above "Common commands" reading "GitHub 免费开源项目：https://github.com/jimu2000/M_Bot", with the URL shown as plain text, and in English "GitHub Free Open Source Project: https://github.com/jimu2000/M_Bot"
- Desensitization fix (found by the 2.02 desensitization sweep): the rescue netboot wizard's TFTP node list hard-coded the real IP of the user's own Singapore node (it already went into the public repo with 2.01); fixed by keeping only the example placeholder 203.0.113.10 in code and reading your own node from the MBOT_TFTP_NODE environment variable (add it to /root/.rbot-clone.env on .160 at deploy time to restore the old default); the web API's ?tftp_server= override still works. Note: the IP is still in the 2.01 GitHub history — only a history rewrite removes it for good (same pending item as the planned Git history rewrite)
- Bugs found by the ARM panel full-function test (test target was the v1.43.0 ARM panel; the same bugs existed in 2.01): (1) high: STOPPED instances vanished from the instance list — the backend GET /api/instances already supported state=all (all non-TERMINATED instances), but the frontend's instance-list load and CPU-model refresh both called the default state=running, so a stopped instance disappeared from the list and could not be started again from the panel (instances also vanished mid-resize while non-RUNNING); fixed by using state=all in both places, with a Start button on STOPPED cards; (2) medium, verified only: CPU filtering still letting grabs through — already fixed in 2.01 (no guard key sets cpu_unverified and the notification says "CPU 未验明", panel grabs default to 7J13-only); this release adds a test proving it still holds: with an explicit target_cpu=7J13 and no guard key, the Grabber gets no CPU filter, make_cpu_check_hook is not called, the notification marks cpu_unverified, and the task log says the guard key is not configured; (3) low: the Web SSH terminal banner still said the old brand "R-BOT · Web SSH", redrawn as a five-letter M_Bot banner (M_BOT_ART) with the banner line "M_Bot · Web SSH"; no R-BOT/R_Bot text remains in non-test source; (4) low: cloud-report boot-volume counts lagged — GET /api/cloud-report/summary cached the whole report for 60 seconds and clear_cloud_report_cache was only ever called by tests, never by mutations, so terminating an instance still showed 4 volumes / 188 GB for up to a minute; cache invalidation is now added at every mutation that affects the report (instance batch ops / single start-stop-restart / terminate, restore from backup, boot-volume resize and delete, successful grab, successful boot swap), alongside the existing instance/volume cache clears; (5) low: deleting a backup required confirm in the query string — DELETE /api/boot-backups/{backup_id} now accepts both ?confirm=true and a {"confirm": true} body, and when neither is given the 400 error names both ways; (6) low: no upfront check that the image architecture matches the shape when creating a grab — an aarch64 image with an E2 shape only failed at OCI with InvalidParameter; now the frontend checks the dropdown option's data-arch against the shape's required architecture before submitting (E2 needs x86_64, A1 needs aarch64) and the backend POST /api/create checks again after resolve_image_id (placeholder arch comes from its suffix with no OCI call; a real OCID is looked up via image get and its architecture inferred from the display name — OCI's image get has no architecture field, and when it cannot be inferred the creation is not blocked), returning a plain 400 naming the image architecture, the architecture the shape needs, and whether to change the image or the shape, without starting the Grabber

**Verified**: full suite 2941 tests pass (2.01 baseline 2893, net +48). New cases: bind-domain dispatch 4, safe_edit 6, error_handler 2, bot DNS annotation and delete 12, web DNS annotation 6, login-page and /start GitHub 3, STOPPED instance list 1, CPU-filter verification 1, cloud-report cache invalidation 1, delete-backup body confirm 2, image-architecture check 10, TFTP node env var 1. This release only touches the local tree; it is not deployed to any server yet, so live verification is still pending after deploy (bind a domain from a phone and confirm the subdomain input gets a response; check the /domain list shows bound annotations and can delete; a stopped instance stays in the list and can be started; an aarch64 image with an E2 shape is rejected at submit).

## [2.01] - 2026-11-25

- Web language switch (Chinese / English): the language dropdown at the top-right of the top bar (Simplified Chinese / English) now actually works; Chinese is the default. Switching changes the whole site's copy (navigation, buttons, titles, hints); the login page has no top bar, so it gets its own switcher in the card, sharing the same logic. The choice is stored in localStorage (key mbot-lang) and survives refreshes. JS-rendered copy switches too (the cloud-report table and host cards on the overview page, instance state badges and the selected-count on the instances page, and more), covering the login, overview / cloud report, instances, create, and settings / navigation pages. The dictionary lives in web/static/js/i18n.js, with data-i18n attributes marking static copy. Three language options: Chinese, English, and "follow browser language" — in follow mode the page follows navigator.language, recognizing only zh-prefixed as Chinese and en-prefixed as English, and falling back to English for any other language (Russian, Japanese, French, etc.), so the panel never shows an unsupported language; manually picking Chinese or English pins that choice and stops following; the follow mode itself is also persisted in localStorage and still follows after a refresh
- Bot language switch (Chinese / English): the Telegram bot supports Chinese/English switching, via the "🌐 Language" button at the bottom of the Oracle menu and the main menu, plus the /lang command. The per-user choice is stored in tgbot/data/user_lang.json (0600, atomic writes), one file per user. Switching changes menu, prompt, and button copy, covering the main menu, Oracle menu items 1–44, instance / boot-form / quota / traffic / billing flows. About 60 deep strings (fmt_error, disk/backup, GFW, Bark, domain/CF, object storage, background notifications) are not yet covered. The web and bot language preferences are stored independently and are not forced to sync. The dictionary lives in tgbot/i18n.py
- Cloud report states in Chinese: instance states in the overview cloud-report table now render in Chinese — RUNNING → 运行中 (running), TERMINATED → 已终止 (terminated), STOPPED → 已停止 (stopped), PROVISIONING → 创建中 (provisioning), STARTING → 启动中 (starting), STOPPING → 停止中 (stopping), TERMINATING → 终止中 (terminating), CREATING → 创建中 (creating); unknown states fall back to the original text, and English mode shows the original OCI codes. Display only — the data and GET /api/cloud-report/summary are unchanged. Instance-page state badges switch to Chinese too
- GitHub links completed: the login page gains a “GitHub开源主页” (GitHub Home) line above “默认凭据请查看部署文档” (see the deployment docs for the default credentials), linking to https://github.com/jimu2000/M_Bot in a new tab; the footer “GitHub 项目” (GitHub Project) link stays. The GitHub icon (Feather outline) in the inner-page top bar is visible on both mobile (≤900px) and desktop and is never squeezed out or hidden by other top-bar elements (on mobile the logo subtitle collapses, spacing shrinks, and the selects get a max width; the icon is flex-shrink: 0)
- Static files stop being cached: the /static mount now sends Cache-Control: no-cache, so browsers revalidate on every load instead of heuristically caching stale JS/CSS (this is why the v1.45.0 cloud-report table stayed invisible on mobile Safari for hours after the deploy); no need to touch the ~20 static references in templates or add ?v= version strings
- Auto-upgrade cron dedup: setup_auto_upgrade() now also removes old mbot auto-upgrade entries from root's local crontab when it writes /etc/cron.d/mbot-auto-upgrade (daily 4 AM), so the upgrade no longer runs twice a day; only lines mentioning both auto-upgrade and mbot/install.sh are dropped, everything else is left alone, and a missing crontab command or empty crontab is not an error. MBOT_INSTALL_NO_AUTO_UPGRADE still just removes the cron.d file and skips.
- Boot-success notification now shows the CPU model: the model measured by the guard SSH check during the grab is carried down (new cpu_model field on GrabResult, persisted through the task record on restart recovery), so the "cpu type" line shows the specific model when available (e.g. "cpu类型：AMD EPYC 7J13", A1 shows "cpu类型：Ampere Altra") and falls back to AMD/ARM when unknown; the CPU-unverified reminder line is kept. Changed on both the Bot and the web panel, with Telegram and Bark sharing the same text.
- Panel grabs filter by CPU by default: when a web E2 grab task is created with target_cpu left empty, _run_grabber now fills it from cpu_policy.json (default 7J13-only) instead of grabbing unfiltered, and the same default goes into grab_spec so restarts keep filtering; after grabbing, M_Bot verifies the CPU itself over guard SSH and auto-releases non-7J13 machines (e.g. 7742/7551) with the boot volume permanently deleted, no manual SSH check needed; the kept machine's model is written to the task log (done event) and the cpu_models.json cache; when the guard key is not configured, both the task log and the notification now clearly say the CPU is unverified (fixed the gap where the cpu_filter_unavailable event did not set cpu_unverified, on both the Bot and the web); the create form hint no longer says "leave empty for no filtering". The Bot's E2 default was already 7J13-only (TARGET_CPU), no change needed there.
- Bind a domain from instance details (bot + web): instance details gain a "🌐 Bind domain" entry. It first picks a root domain from the configured Cloudflare Zones (reusing the existing /domain and cf_config credentials), then takes a subdomain (www/app, or @ for the root) and creates an A record pointing at the instance's public IP. After creation it echoes "full domain → IP" and notes that DNS needs some propagation time. When the instance has no public IP the web button is hidden and the bot says it cannot bind. Subdomains allow only letters, digits, -, _, . or @. When an A record with the same name already exists, it says so if it already points at the same IP, and asks for confirmation before overwriting when it points elsewhere. Missing Cloudflare credentials go through the existing credential flow
- Whole-repo comment cleanup: per the user's comment rules, unnecessary comments are removed repo-wide (comments restating obvious code, version-history logs, comments duplicating the code itself), and comments that must stay are kept in both Chinese and English (config items, values the user must fill in, paths to change at deploy time, complex-logic explanations, security warnings, API quirks). 187 files touched, 1328 comments deleted, 1527 kept and bilingualized. Test files follow the same rules
- Rescue feature (bot + web, menu 44 / Rescue page, verified against ARM test results): ① safe restore with create-before-delete (backup → restored volume → native swap → delete old volume, with a quota pre-check that blocks when short; the old volume is not auto-deleted when it is a backup source); ② diskless rescue reworked per test results into a "rebuild a new instance from backup" wizard (no "reattach a disk to save the original instance" button: direct start returns 409, there is no API to create a boot volume from an image, restored/cloned volume attach is always rejected with 409, and the native swap API returns 400 without a mounted volume — all five in-place paths failed; the flow picks a backup → restores it to a volume → launches a new instance reusing the original SSH keys, and states the cost: new OCID, new public IP, original instance cannot be revived; with no backup it says plainly that recovery from nothing is impossible); ③ orphan-disk detection and cleanup using the verified rule (no attachment record at all + restored from a backup; AVAILABLE alone is not enough), showing reclaimed quota with one-tap delete; ④ netboot rescue wizard (disk present, system bricked: create console connection, EFI Shell commands, nearest TFTP node, netboot.xyz menu guide, Alpine live system → DD steps, with ESC timing and menu navigation marked manual); ⑤ pre-rescue checks (before start / restore / delete / terminate: instance state, boot volume attachment, quota headroom, backup-source relations, with a can/cannot verdict, what is missing, and the risks); ⑥ serial-console management (one-tap create/delete with the SSH command shown; RSA keys only, ed25519 is rejected by OCI); deleting a volume first checks whether it is a backup source and blocks with an explanation when it is. Core logic lives in oci/rescue.py (shared by web and bot) and oci/console.py, fully covered by mocked tests

**Verified**: full suite 2893 tests pass (v1.45.0 baseline 2691; 2.01 adds 202: rescue 82, web i18n 36, bot i18n 42, bind domain 32, static no-cache 1, auto-upgrade dedup 1, CPU model notification 3, CPU filter default 5). This release only touches the local tree; it is not deployed to any server yet, so live verification is still pending after deploy (switch to English on a phone and check the whole site, the Chinese cloud-report states, and the GitHub links on mobile and desktop; walk the safe-restore and diskless-rescue flows on a real machine).

## [1.45.0] - 2026-11-25

Note: this release was originally numbered v1.44.0 and renumbered to v1.45.0 before publishing.
- All 5 issues found by the install tests are fixed: (1) the installer systemd units now set MBOT_WEB_ENV_FILE (the panel and bot read/write the env file at the installer's /root/.mbot.env instead of a stray ~/.rbot-clone.env, fixing delete-account reporting env_removed:false); (2)(3) the multi-account compartment gap and the install.sh:422 placeholder: the installer now derives the compartment from the tenancy in the OCI config instead of writing the ocid1.tenancy.oc1..xxxxxxxx placeholder, and the panel/bot treat a placeholder as unconfigured and derive it from the tenancy, so accounts without a configured compartment show up after a clean install plus copying oci_config; (4) cfdns propagation checks now use public resolvers: lego gets --dns.resolvers 1.1.1.1:53,8.8.8.8:53 (instead of the local OCI resolver 169.254.169.254 with its 1800-second negative cache), the error message is corrected (no longer misleadingly blaming Token permissions), and lego output is kept in a log file on failure; (5) update mode now also reads CERT_METHOD from MBOT_INSTALL_CERT_METHOD instead of defaulting to http
- All 3 issues found by the panel vulnerability test are fixed: (6) M-1 FastAPI auto docs exposed without auth: create_app() now sets docs_url/redoc_url/openapi_url to None (/docs, /redoc and /openapi.json all return 404); (7) L-2 session cookie missing the Secure flag: the login session cookie sets Secure based on the request scheme (Secure over nginx HTTPS, unset for direct http and tests, otherwise loopback logins lose the cookie); (8) L-4 422 validation errors leaking server paths: the validation-error handler now returns only field-level errors, with no file paths or line numbers
- (9) Ubuntu nft empty-table fallback: when install.sh finds an empty nft filter table (no input chain/rules) it falls back to enabling UFW; a table that already has rules only gets a warning; IPv6 filter support added too
- (10) Two HTML terminate-confirm dialogs did not escape instance names: the single-terminate (tgbot/handlers.py) and batch-terminate (tgbot/handlers_extra.py) instance names now go through html.escape
- (11) nginx security hardening synced into the repo (already done locally in v1.43.0): security response headers (HSTS/X-Frame-Options and friends), scanner user-agent blocking, and Cloudflare real-IP handling, all three variable groups wired into the three HTTPS vhost templates
- (12) GitHub project link added to the web panel (https://github.com/jimu2000/M_Bot): a "GitHub 项目" text link under the login box on the login page, and a GitHub icon (Feather outline, matching the other icons) in the top bar on inner pages; both open in a new tab and stay low-key
- (13) Cloud report as a table: the cloud-report card on the overview page used to render one dense paragraph per region, which looked messy on phones; it is now an HTML table (Region / Instances / Boot volumes / A1 / E2 columns: region shows the Chinese name plus the region code, instances split RUNNING/TERMINATED, A1 shows used/limit for both cores and memory), one row per region, and a failed account still gets a row; the table sits in a .table-wrap so small screens scroll horizontally instead of overflowing

**Verified**: full suite 2691 tests pass (16 new test methods: 5 installer-script, 4 compartment-config, 3 panel-security, 2 HTML-escaping, 1 GitHub link, 1 cloud-report table; 5 existing tests also got PROFILES patches so they no longer depend on the local oci_config). This release only touches the local tree and staging; it is not deployed to any server yet, so live verification is still pending after deploy.
- The install script's nginx repo switches from the stable branch to the mainline branch: fresh installs now get the newest nginx (e.g. 1.31.10, matching the existing four machines) instead of stopping at stable's 1.30.x; the repo key and signed-by are unchanged (mainline and stable share the same signing key), and a failed repo setup still falls back to the distro's nginx

**Verified**: full suite 2672 tests pass (test_install_script.py gains a mainline-path assertion). Live verification: one machine was switched the same way from stable 1.30.8 to mainline 1.31.10, nginx -t passes and the panel login page returns 200.

## [1.42.3] - 2026-11-25
- Corrects a false statement in the v1.42.2 changelog: iOS one-time-code autofill does not scan only SMS and email — it can pick the code up from a Bark push notification (verified live on the evening of 2026-11-24, when the keyboard showed "来自 Bark" plus the 6-digit code and tapping it filled the code in); the v1.25.0 claim that iOS picks the code up from the notification center was correct, and v1.42.2 was wrong to call it a misjudgment. A code comparison confirmed it: the login-page input, the login-page JS, and the Bark push title and body are all byte-identical between v1.25.0 and now, so the suggestion not appearing on the morning of 2026-11-25 is iOS-side flakiness (the suggestion bar sometimes does not refresh when the keyboard is already up as the code arrives), not a code regression; the same false statement in the auth.py comment is fixed too

**Verified**: documentation and comment changes only, no logic changes; full suite 2672 tests pass.

## [1.42.2] - 2026-11-25
- Tapping the Bark login-code notification now logs in directly: send_bark gains a url parameter (included in the payload before encryption when passed, absent from the payload when not, so existing callers are unaffected); the login-code Bark push carries the same one-tap login link as the message body, so tapping the notification opens the panel already logged in instead of typing the 6-digit code
- iOS one-time-code autofill can pick the code up from a Bark push notification (verified live on the evening of 2026-11-24: the keyboard showed "来自 Bark" plus the 6-digit code, and tapping it filled the code in), though it does not fire every time; tapping the notification to log in directly remains the most reliable zero-typing path (note: v1.42.2 originally misstated this as "only scans SMS and email"; corrected in v1.42.3)
- Note: 1.42.1 was never released (it was reserved for the Markdown start-confirmation regression fix, but the audit found no leak in the code and no fix was needed, so the version was skipped)

**Verified**: full suite 2672 tests pass (4 new: 2 in tests/test_bark.py — the payload contains url when passed and omits the url key when not; 2 in tests/test_phase5e_auth.py — the login-code Bark url is the magic login link, and the body and the url are the same link). Live verification: after deploy, tap a Bark login notification on the phone and confirm it opens the panel logged in.

## [1.42.0] - 2026-11-25
- CPU filter tightened to 7J13 only (user decision: both 7551 and 7745 are released and re-grabbed): the default allowed list in tgbot/cpu_policy.py is now ["7J13"], and the confirm-page button and target-CPU text follow the configured rules ("only grab 7J13"); an existing rules file (tgbot/data/cpu_policy.json) still takes precedence over the default and must be updated on deploy
- Grab tasks are now persistent and resume after restart (updating no longer loses a running grab): when grabbing starts, the full spec (profile/region/compartment/all Grabber parameters/notification info/already-grabbed list) is written to the shared task record as grab_spec and updated after each successful grab; on restart, same-origin grabbing tasks with a spec automatically resume (remaining count = total - already grabbed), tasks that already grabbed enough are marked done and the success notification is sent, tasks without a spec or from the other side are not resumed; the Telegram token is read from the environment at resume time and never stored on disk; the tasks file is chmod 0600 on every write (same precedent as bark.json), and the API task view strips grab_spec
- The Grabber's user_data temp file is recreated if deleted externally (previously created only once, so a deleted file made every launch fail)
- Notification observability: every grab-success notification now leaves one result line in the task log whether it succeeded or not ("通知：Telegram 已发送，Bark 已发送" / "Bark 未配置，已跳过"; partial failures keep the existing wording), and the bot also logs it (into the journal), so a lost push can be traced
- The instances page no longer shows stale empty CPU models: the instance list request no longer uses the 15-minute frontend cache (fresh fetch on every page visit; the 30-second server cache is enough protection); after rendering, if any instance still has no CPU model, the list is re-fetched once after ~5 seconds with refresh=1 and only the CPU model text on the cards is updated
- The boot volume dialog on the instances page gains a "恢复启动卷" (restore boot volume) button (next to "备份启动卷", before "取消"), reusing the existing boot-volume restore flow and backend from the instance detail page

**Verified**: full suite 2668 tests pass (17 new: tests/test_v1420_grab_resume.py, covering spec persistence, resume with the correct remaining count, already-complete marking, 0600 permissions, no-spec/other-origin not resumed, user_data recreation, and the three notification-log cases; plus updated default assertions and text assertions for the CPU rule). Live verification: the frontend CPU refresh and the restore button need checking in the browser; restart the bot after deploy to verify grab resume.

## [1.41.1] - 2026-11-24
- The orphans endpoint no longer runs its own queries: it reuses the boot volume list cache and filters it (orphan = AVAILABLE and not attached, matching oci.volume.list_orphan_boot_volumes), cold load ~5.0s → ~3.4s; volumes page cold total ~13.5s → ~5.1s (as stated in v1.41.0)

**Verified**: full suite 2651 tests pass (the orphans cache test in tests/test_phase5e_shapes.py became 2 tests: shared list cache, refresh bypass; test_orphans in tests/test_web_api.py updated to the new logic). Live verification: /api/volumes/orphans cold load re-measured after deploy.

## [1.41.0] - 2026-11-24
- CPU filter rules are now configurable and actually wired in (the "only grab 7J13" option used to be a no-op — the hook was never injected): new tgbot/cpu_policy.py, rules live in tgbot/data/cpu_policy.json (0600), default keeps only 7J13/7745; at boot the guard public key is merged into the launch SSH keys (new combined_boot_keys), the Grabber gets a cpu_check_hook, and a grabbed instance is SSH-checked first — a non-matching CPU is released (boot volume permanently deleted) and grabbing continues; when the CPU cannot be verified the instance is kept and marked "unverified" instead of being released; without a guard key configured, grabbed instances are kept and the detail says so; bot and web grabbing changed together, button text follows the configured rules
- Grabber target_cpu now accepts a list (any listed model matches); a hook returning None or raising is treated as unverified and the instance is kept
- Bot and web tasks are now shared: both sides use web/data/tasks.json (new tgbot/task_store.py, file-locked read-modify-write, atomic replace on write), so the bot sees web grabbing tasks and the web sees bot tasks; cancelling the other side's task writes cancel_requested, and the other side's watcher thread sets the stop event to stop grabbing; task IDs carry an origin prefix (bot-g1 / web-g1), legacy unprefixed IDs still work; on startup only the current side's unfinished tasks are recovered
- E2.1.Micro shape display now shows "永久免费" (permanently free) in bot details/presets and web instance cards/detail dialogs
- Web loading optimization: boot volume and orphan volume lists get a 30-second server cache (same as the instance list, refresh=1 bypasses it, cleared after resize/VPU change/delete/terminate/restore); attachment and instance-name lookups now run in parallel; the instances page fetches instances and volumes in parallel, the volumes page fetches volumes/orphans and backups/quota in parallel; measured cold loads: instances page ~9.8s → ~4.7s, volumes page ~13.5s → ~5.2s

**Verified**: full suite 2650 tests pass (74 new: tests/test_cpu_policy.py 24, tests/test_task_store.py 27, tests/test_web_grab_cpu.py 4, the rest in test_grabber.py, test_notify.py, test_v1220_cpu_tasks.py, test_tgbot_instance_list.py, test_web_api.py, test_phase5e_shapes.py; plus 3 racy tests fixed: waits added before reading Grabber call_args after POST, and a wait moved inside the Grabber patch). Live verification: /api/instances and /api/volumes timings measured on the deployment host before deciding the approach; cold-load timings re-measured after deploy.

## [1.40.1] - 2026-11-24
- Fixed the missing "grab started" confirmation after tapping "Start grabbing": when a root password was set, the whole reply was parsed as Markdown, and the underscore in the instance name (AMD_2026-11-25) was treated as an italic marker, so Telegram rejected the message (Can't parse entities); the dynamic instance name is now escaped with escape_markdown before being put into the message, while the name in the task list stays unescaped

**Verification**: full test suite passes, 2576 tests (1 new regression test: test_start_reply_escapes_underscore_in_name in tests/test_boot_form_handlers.py).

## [1.40.0] - 2026-11-24
- Quick boot presets (menu 17) now support editing and deleting: each preset row has three buttons — 🚀 boot / ✏️ edit / 🗑️ delete; editing loads the preset back into the boot form (new `form_from_preset` in tgbot/boot_form.py, inferring cpu/os from shape and image for presets saved by older versions) and saving updates the preset in place, keeping its name; deleting asks for confirmation first, then refreshes the list with a deleted notice
- Fixed the boot-success notification showing "fetching" for the IP forever: it used to retry only 3 times, 3 seconds apart (giving up in under 10 seconds), while OCI often takes 30+ seconds to assign a public IP; it now waits up to 60 seconds (new `wait_for_public_ip` in oci/network.py), and if the IP still is not there, the notification goes out anyway and a background follow-up thread keeps waiting (new `start_ip_followup` in tgbot/notify.py, up to 10 minutes), sending a Telegram message as soon as the IP appears; bot and web grab flows changed together
- Fixed root login not working on freshly launched Ubuntu instances: the user-data used to only set the password and `ssh_pwauth`, but Ubuntu images default to `disable_root: true` (the root key gets a forced command printing "Please login as the user ubuntu") and `PermitRootLogin` blocking password logins; `build_user_data` now adds `disable_root: false` plus an sshd drop-in (`PermitRootLogin yes`) and reloads sshd (trying both `ssh` and `sshd` service names); bot and web share the same function, so one change covers both

**Verification**: full test suite passes, 2575 tests (21 new: 4 in tests/test_network.py, 3 in tests/test_notify.py, 2 in tests/test_web_grab_notify.py, 4 in tests/test_boot_form.py, 8 in tests/test_boot_form_handlers.py; 12 existing tests updated to mock the new logic). Live verification: `wait_for_public_ip` returns immediately for an instance that already has an IP; the root-login fix was verified manually on a fresh Ubuntu instance (removing the forced command and setting `PermitRootLogin yes` makes `ssh root` work); the generated user-data was validated by parsing it as YAML.

## [1.39.0] - 2026-11-24
- New scheduled auto-upgrade: installation now sets up a cron job (daily at 04:00 server local time) running `install.sh auto-upgrade`; it first compares the local and remote versions, and when already up to date it only logs and touches no service; when a new version exists it pulls the code, installs dependencies, restarts only running or enabled services, and runs a health check
- Auto-upgrade failures send a Bark notification (titled "M_Bot 自动升级失败", with hostname, reason, and log path; silently skipped when Bark is not configured or disabled, and notification errors never affect the upgrade flow)
- The cron job runs install.sh from a temporary copy, so a git pull updating install.sh itself cannot rewrite the running script; setting `MBOT_INSTALL_NO_AUTO_UPGRADE` skips the cron setup
- New subcommands `install.sh auto-upgrade` (run one check-and-upgrade cycle manually) and `install.sh setup-auto-upgrade` (enable the cron on an existing install without touching services); uninstall removes the auto-upgrade cron and log
- Fixed health_check treating a deliberately disabled bot as a failure: when the bot unit exists but is disabled and inactive it now only logs an info line and skips the check, instead of warning and failing (otherwise hosts with the bot intentionally stopped would report a false failure on every auto-upgrade)

**Verification**: full test suite passes, 2554 tests (9 new in tests/test_install_script.py). Live verification: running auto-upgrade manually on the test host reports "already up to date" without restarting services; another host upgraded from v1.31.0 via upgrade to v1.39.0 and then enabled the cron.

## [1.38.1] - 2026-11-24
- Fixed deleting an account leaving an extra blank line in the config file: remove_oci_profile unconditionally appended a `\n` after removing the last section, while adding a section never adds a separator blank line, so the file no longer matched the original byte-for-byte; removal now only drops the section's own lines and leaves everything else untouched
- Fixed upgrades reviving a deliberately stopped service: `install.sh upgrade` restarted every installed service unconditionally, bringing back a bot that had been stopped and disabled; it now only restarts services that are running or enabled

**Verification**: full test suite passes, 2545 tests (2 new: 1 in tests/test_web_oci_profiles.py covering byte-identical config after an add-then-delete round trip; 1 in tests/test_install_script.py covering upgrades restarting only running or enabled services). Live verification on the main host: verify_v138.py passes 19/19 (fake account FAKEDEL01 added then deleted, config and env both restored byte-for-byte).

## [1.38.0] - 2026-11-24
- Configured accounts on the cloud accounts page can now be deleted: each row gains a delete button (with a confirmation prompt), removing the config section and the compartment variable; the private key file is only removed when no other account shares it
- Deletion guards: DEFAULT is the default account and cannot be deleted; at least one account must remain; the panel list updates immediately after deletion, and the bot needs a restart to pick it up
- New endpoint DELETE /api/profiles/{profile}; new helpers remove_oci_profile in tgbot/handlers_client.py (text-level section removal, everything else preserved byte-for-byte) and remove_env_file_vars in tgbot/config.py

**Verification**: full test suite passes, 2543 tests (7 new in tests/test_web_oci_profiles.py covering successful deletion with config/env/key cleanup, keeping a shared key, and rejection of deleting DEFAULT/the last profile/an unknown profile/unauthenticated requests, plus section removal leaving other sections and the [cloudflare] section intact). Live verification on the main host: a fake account FAKEDEL01 was created and then deleted through the API; its config section, env variable, and key file were all cleaned up, and the three real accounts were verified unchanged item by item.

## [1.37.2] - 2026-11-24
- Configured accounts on the cloud accounts page can now upload their private key directly: the account list gains a key status column (ready/missing) and a private key upload control, writing the file to the account's key_file path (mode 600); accounts sharing one key are all fixed by a single upload; a key that is already in place can be replaced (with a confirmation prompt)
- New endpoint POST /api/profiles/{profile}/key: the upload must be a private key file (rejected above 16KB) and the target path must be under home; GET /api/profiles now also returns key_file and key_ready per profile

**Verification**: full test suite passes, 2536 tests (8 new in tests/test_web_oci_profiles.py covering list key status, successful upload with mode 600, and rejection of non-key/oversize/out-of-home/unknown-profile/unauthenticated uploads). Live verification on the test host: after the panel restarted, all three accounts showed their keys as missing, the upload endpoint worked, and the cloud report recovered once the key was in place.

## [1.37.1] - 2026-11-24
- Fixed a re-run of the one-click install deleting the bot service: the token check in update mode used `.+`, which BRE grep treats as a literal plus sign and never matches, so every re-run concluded "no token" and stopped and removed the bot unit; the check now uses BRE-safe `.`, and the bot is kept when its unit still exists but the env file is lost
- Fixed the panel still serving old code after a re-run install: the units were applied with `systemctl enable --now`, which does not restart an already-running service, so the old process kept serving old code (that was the /accounts 404); units are now applied with enable + restart

**Verification**: full test suite passes, 2528 tests (3 new in tests/test_install_script.py covering the BRE-safe token check, restart in unit setup, and keeping the bot when the env file is lost). Live verification on the test host: re-running the fixed one-click install restored the bot unit, and after the panel process restarted /accounts works and the certificate page correctly reads the certbot certificate.

## [1.37.0] - 2026-11-24
- The certificate page can now read certificates installed by install.sh: the status endpoint looks for a certificate in the order acme.sh → certbot → lego → nginx (the file its ssl_certificate points at), and the response carries a new source field naming where it came from; for the nginx source the domain is derived from the path or the certificate CN. Renewal still only goes through acme.sh; for certbot/lego certificates the page notes they auto-renew and the panel does not renew them
- The acme.sh scan skips ca.cer (the intermediate certificate) and reads only the leaf certificate; ECC certificate directories named `<domain>_ecc` no longer show the _ecc suffix in the domain, and `?domain=` finds certificates in _ecc directories too
- One-click install now guarantees the latest version: install.sh has a shared update_code function used by both install (when code already exists) and the upgrade subcommand — git pull first, falling back to fetch + reset to force-sync to the latest version; if both fail the install stops with an error instead of silently continuing with old code; the installed version is printed after install and upgrade
- One-click install is now fully non-interactive: writing the nginx repo keyring uses `--batch --yes`, so an existing keyring file is overwritten without an Overwrite prompt that used to hang a re-run before the code was even updated
- README token tutorial gains a warning: run only one bot per token, on one machine — two machines sharing a token compete for updates

**Verification**: full test suite passes, 2525 tests (9 new in tests/test_phase5e_cert.py covering the certbot/lego/nginx fallbacks, the domain parameter, source precedence, renewal still being acme.sh-only, and ECC directories skipping the intermediate certificate; 4 new in tests/test_install_script.py covering the update_code fallback and loud failure, shared use by install and upgrade, the printed version, and the non-interactive keyring write). Live verification: on the deployment host the certificate status still reports source acme.sh, with the _ecc suffix dropped from the domain and the validity now showing the leaf certificate (2027-02-21); on the test host the one-click install was re-run end to end with stdin closed — no prompts, finishing at v1.37.0, and its certificate page correctly reads the certbot certificate.

## [1.36.0] - 2026-11-24
- New Accounts page on the web panel (/accounts): adding a tenant now works like the bot's /uploadapi — paste config text or upload a config file for auto-parsing (R-Bot format supported, several accounts at once), existing accounts are filtered out automatically; private keys (.pem) are uploaded in the confirm step (contents validated as a private key, saved with 0600 permissions, path forced under home), never through Telegram; a [cloudflare] section in the config is saved too; compartment env vars are added automatically; new accounts take effect after restarting the bot and the panel
- Two-step parse-then-confirm flow: parsing returns a pending_id (valid 10 minutes, single-use); nothing is written until confirmation; responses never echo OCIDs, fingerprints, or Cloudflare credential values
- README tutorial addition: open t.me/<username> directly when you can't find your own bot

**Verification**: full test suite passes, 2512 tests (21 new in tests/test_web_oci_profiles.py, covering parsing, filtering, R-Bot format, no credential echo, pem upload and validation, path traversal protection, single-use confirmation, and unauthenticated rejection). Live verification on the deployment host passed 13/13: the /accounts page, parsing, and committing a fake account (config + 0600 pem + env var all written), then fully restored byte-identical.

## [1.35.5] - 2026-11-24

### Docs
- README now has a step-by-step OCI API key tutorial (Chinese and English): the config text and the private key go through two different channels (config to your own bot's private chat via `/uploadapi`, private key via scp to the server, never through Telegram); key-pair generation, uploading the public key in the Console, where to find the tenancy/user OCIDs, the config format (key_file can be any path under `/root/`; the `oci setup config` default `~/.oci/oci_api_key.pem` needs no moving), multiple accounts and R-Bot format, how to find your own bot, the old `~/.oci/config` as fallback only, and how to verify; install.sh header comment updated to match.

### Verification
- Full test suite: 2491 tests, all passing (docs and comment changes only, no code behavior changed).

## [1.35.4] - 2026-11-24

### Docs
- README now has a step-by-step Telegram bot token tutorial (Chinese and English): how the token differs from the bot ID and the user ID; creating a new bot (`/newbot`) vs. retrieving the token of an existing bot (`/token`); revoking a leaked token in @BotFather; install.sh header comment updated to match.

### Verification
- Full test suite: 2491 tests, all passing (docs and comment changes only, no code behavior changed).

## [1.35.3] - 2026-11-24

### Changed
- Login-page analytics switched back to Microsoft Clarity (configured via the MBOT_CLARITY_ID environment variable; not loaded when unconfigured; Clicky code and MBOT_CLICKY_ID removed).

### Verification
- Full test suite: 2491 tests, all passing (the 2 login-page analytics tests swapped over: Clarity loaded / unconfigured cases).
- Live verification: deployed to .160 and restarted the panel; the login page HTML contains the Clarity script and no Clicky code.

## [1.35.2] - 2026-11-24

### Added
- Bark notification when a boot-volume backup finishes creating (user request, 2026-11-24: both backup creation and restore should send Bark notifications; previously backup creation only replied "creating" on Telegram and never notified on completion): new module `tgbot/backup_watch.py` — after a successful create, a daemon thread polls the backup status (new `wait_for_backup_available` in `oci/boot_backup.py`, every 30 seconds, up to 30 minutes) and sends a dual-channel Telegram + Bark notification "💾 Backup completed" via notify_dual once the backup is AVAILABLE; a terminal failure state sends "Backup failed", a timeout sends "Backup timed out", and a polling error sends "Backup status unknown". Shared by all three bot creation points (menu 7, instance detail "Boot volume backup", delete-oldest-then-create when full) and the web create endpoint (POST /api/instances/{id}/boot-backups); no thread is started when the token, chat id, or backup id is missing. Restore notifications already existed (notify_dual dual channel); the Bark channel was verified live during this work (test push succeeded).

### Verification
- Full test suite: 2491 tests, all passing (13 new in v1.35.2: 5 for the wait helper — immediately available, CREATING/REQUEST_RECEIVED then available, FAULTY terminal state, timeout; 8 for the watch thread — success/failure/timeout/error notification copy, no start when the token/chat id/backup id is missing, daemon thread start; 8 existing creation tests now mock the watcher and assert it is started).
- Live verification: deployed to .160 and restarted the bot and the web panel; services are up with no errors in the log. The Bark channel was verified with a live test push using the production code path.

## [1.35.1] - 2026-11-24

### Changed
- The "no available backup" message now explains that old backups still exist (user request, 2026-11-24: after deleting an old boot volume, the bot said the instance had no available backup and the user thought the backup had been deleted): in-place restore only lists backups of the current boot volume, so a backup of the pre-swap old volume is not listed — the message was misleading. Bot: when the current volume has no available backup, it also checks for available backups of other volumes in the account and appends a note ("Note: the account still has N available backups of other boot volumes (e.g. backups of the old volume before the swap); deleting a volume does not delete its backups; in-place restore only lists backups of the current boot volume; old backups can be restored as a new instance from the backups page"); a failed lookup leaves the main message unchanged. Web: GET /api/instances/{id}/boot-backups returns other_available_count when the current volume has no backups, and the restore dialog shows the same note.

### Verification
- 4 new tests in v1.35.1: bot note shown when other backups exist, main message unaffected when the lookup fails; web endpoint returns other_available_count, 0 when the lookup fails.

## [1.35.0] - 2026-11-24

### Changed
- Boot-volume restore now deletes the old boot volume after a successful swap instead of leaving it behind (user request, 2026-11-24): `swap_boot_volume` deletes the replaced old volume once the swap is confirmed and the instance is back to its pre-swap state (OCI refuses to delete a volume that is still attached, so a still-attached old volume can only fail the delete, never be deleted by mistake). A delete failure does not affect the restore result: the return value marks `old_volume_deleted=False`, and both the bot and web success messages warn the user to delete the old volume manually. The bot's selection and confirmation pages (step ⑤ "delete the old boot volume" added to the flow) and the web instance-page hint are updated to match; the success message's old-volume line now reads "deleted, quota freed" or "auto-delete failed, please delete manually".

### Verification
- Full test suite: 2474 tests, all passing (3 new: the happy path asserts the delete-old-volume command (old volume, not the new one, with --force, after the swap) and `old_volume_deleted=True`; a delete failure still succeeds the restore and marks False; bot and web delete-failure warning copy; 4 existing tests updated for the new behaviour: happy-path step count 2→3, bot and web success-message assertions now expect "deleted, quota freed").
- Live verification: deployed to .160 and restarted the bot and the web panel; services are up with no errors in the log. A real swap-through verification needs the next actual restore (the old volume should be deleted automatically and the quota freed at the same time).

## [1.34.0] - 2026-11-24

### Added
- One live selection box per chat (user request, 2026-11-24: no more piles of stale menus; delete the redundant selection boxes after tapping). New module `tgbot/menu_tracker.py` manages it centrally: a message with an inline keyboard is a selection box, and sending a new one deletes the old one first; when a result message (no keyboard) is sent, the selection box has served its purpose and is deleted too, leaving only the result. On a button tap, the new message is sent first and the tapped message is deleted after (a reply must go out before its target is gone); when a callback replies more than once, only the first reply uses reply-to, and later ones go through `bot.send_message` (replying to a deleted message returns Bad Request). Deletion failures (message already deleted / older than 48 hours / no permission) are only logged and never break the main flow.
- All three handler modules' `_reply` helpers (511 call sites) are now thin wrappers around `menu_tracker.reply` (with an added `context` parameter); the `/start`, `/oracle`, and `/instances` command entries use the same tracking; the `x` cancel callback clears tracking after editing the message to "Cancelled"; prompt cleanup also clears the live-menu tracking of deleted prompts.

### Changed
- Boot form "cancel custom input": it used to just edit the prompt to say the form was unchanged (the old form was still there). Now the old form is already deleted by the selection-box cleanup when the prompt is sent, so cancelling re-sends the form to continue filling it (same as after a completed input), and the prompt message is deleted by the `_reply` cleanup.

### Verification
- Full test suite: 2471 tests, all passing (new `tests/test_menu_tracker.py`, 10 tests: callback sending a selection box deletes the tapped message and tracks the new one, deletes the previously tracked box, sending a result deletes the box and clears tracking, a second reply in the same callback goes through `send_message`, command-sent box/result tracking, deletion failure does not break the reply, no message and no chat returns None, and `forget`/`delete_live_menu` except behaviour; 16 existing tests updated for the new behaviour: double-reply flows now check the merged channel, the form-cancel test asserts the re-sent form, the monitor-toggle test uses a fresh callback for the second tap, and the direct `_reply` call passes a context; `make_context` fixtures in 11 test files now set both `bot.send_message` and `bot.delete_message` as AsyncMock).
- Live verification: deployed to .160 and restarted the bot; the service is up with no errors in the log. The tap-through cleanup effect needs confirming on the user's phone (instance detail → boot-volume restore → backup list should leave only the current selection box in the chat).

## [1.33.0] - 2026-11-24

### Changed
- Login-page analytics switched from Microsoft Clarity to Clicky (user request, 2026-11-24): when `MBOT_CLICKY_ID` is set, the login page loads the Clicky tracking script (`static.getclicky.com/js`, with the site ID) plus the footer badge link; nothing loads when unset, and no site ID is hard-coded in the public repo. All Clarity code and `MBOT_CLARITY_ID` have been removed (still login page only, not site-wide).

### Verification
- Full test suite: 2461 tests, all passing (the two login-page analytics tests now cover Clicky: script + badge present and no Clarity when configured, neither loaded when unset).
- Live verification: after deploying to .160, the login page HTML contains the Clicky script and badge with no Clarity; the browser end-to-end check could not complete — Clicky's static domain sits behind Cloudflare, which returns a 403 challenge page to datacenter IPs (all of our egress IPs are Oracle datacenter IPs). Real visitors on residential/mobile IPs are unaffected; the visit will show up in the Clicky dashboard when opening the login page from a phone browser.

## [1.32.0] - 2026-11-24

### Added
- Automatic domain binding: before requesting a certificate, the installer checks whether the domain resolves to this machine; if not and Cloudflare credentials are available, it creates or updates the A record to point here (DNS-only, no proxy — the proxy would block http-01 validation) and waits up to 60 seconds for the resolution to take effect. Credential precedence: environment variables > the `[cloudflare]` section of `~/oci_config` (`load_cf_credentials`, shared with cfdns). Without credentials or on binding failure it only warns; certificate issuance then fails and falls back to a self-signed certificate without interrupting the install.
- Post-install self-test: after the health check on a fresh install, four checks run automatically and print their results — certificate (Let's Encrypt issuer + still valid + domain match; handshake only for self-signed), login page (public URL returns 200, with strict chain validation for LE certificates), real login (admin + install password against the login API, expecting 200 and a session cookie), and HTTP redirect (HTTP must 301 to HTTPS when a domain and certificate are present). On http-01 issuance it notes that external port-80 reachability is already proven.

### Verification
- Full test suite: 2461 tests, all passing (4 new: CF credential loading, domain auto-binding, post-install self-test, installer invoking the self-test).
- Live verification: DNS record deleted + uninstall + fresh reinstall on .129, with auto-binding and the self-test verified end to end (see the v1.32.0 install verification notes below).

## [1.31.0] - 2026-11-24

### Added
- The installer now auto-detects the local firewall type: ufw / firewalld / nftables / iptables — whichever is running is used to open SSH + 80 + 443 + 9527. When none is running, ufw is enabled if installed, otherwise skipped (ports are open by default). nft/iptables rules are inserted at the head of the chain (before any drop), with a warning that they do not survive a reboot and a suggestion to switch to ufw or firewalld.
- On OCI instances, the installer opens the cloud security list via the OCI API: after confirming OCI through instance metadata, it uses the OCI CLI to add inbound rules for 80/443/9527 to the primary VNIC's subnet security list (or NSG rules when NSGs are attached). Authentication prefers an existing OCI config file and falls back to instance principals. Already-open ports (including all-protocol rules) are skipped; any failure only warns and prints manual steps without interrupting the install.
- The Telegram bot Token is now optional: leaving it empty installs the panel only, without creating the bot service (panel login and instance management work normally; only Telegram login codes and the bot menu are unavailable). To add the bot later, fill in the Token and user ID in the env file and re-run the installer; management commands (start/stop/restart/status/log/upgrade) only touch services that actually exist.
- Port 9527 occupied by another service no longer blocks the install: the firewall and cloud security list skip it (the reverse proxy cannot bind it, and opening it would only expose the other service); setup_nginx only warns and continues when a domain is given and port 443 is free (the panel uses 443), and fails only otherwise (verified on .129, where the R-Bot client holds 9527).

### Fixed
- The certbot domain certificate flow now uses `certonly --webroot`: previously `certbot --nginx` rewrote the nginx config and reloaded it, which failed when port 443 was already taken by xray — the certificate was issued but discarded. Now certbot only issues the certificate and the installer writes the 443 vhost itself, falling back to port 9527 when 443 is occupied (same as the cfdns path). A deploy hook reloads nginx after renewal, and the port-80 validation entry is kept for renewals.

### Verification
- Full test suite: 2457 tests, all passing (7 new: firewall detection, firewall backends, OCI cloud opening, rule-merging logic, optional Token, certbot webroot fallback, 9527-occupied skip; 5 existing tests updated for the renames).
- The rule-merging logic was run separately against 6 cases, all passing (all-protocol rules count as open, TCP port-range coverage, NSG incremental rules with direction, EGRESS rules ignored, empty rules fully filled).
- Live verification: full install on .101 (domain + Let's Encrypt certificate); see the v1.31.0 install verification notes below.

## [1.30.0] - 2026-11-24

### Added
- Cloudflare credentials can live in the OCI config file: add a `[cloudflare]` section to `~/oci_config` (`/root/oci_config`) with `cf_token`, or `cf_key`/`cf_account_key` + `cf_email`; the bot's `/domain` and the panel DNS page use it. Precedence: saved in bot/panel > OCI config `[cloudflare]` section > environment variables. The credential management page shows the source.
- `/uploadapi` and `/oci` imports preserve the `[cloudflare]` section: if an R-Bot-format config contains it, it is appended to the config file after confirmation (never overwritten if already present; can be saved on its own when there is no new profile; credential values are never echoed in chat).
- The installer reads cfdns credentials from the OCI config first: for `cfdns` verification, precedence is environment variables > the `[cloudflare]` section of `~/oci_config` > interactive input; a Key + email pair is also written to `/etc/lego/env` (previously only a Token was supported).
- nginx is installed latest from the official nginx.org repository: the installer adds the official repo (stable branch, URL switches between debian/ubuntu automatically) instead of the outdated distro package; the key goes to a dedicated keyring with `signed-by` (trusted only for nginx.org packages), and apt pinning (900) keeps future upgrades on the official repo. Falls back to the distro package if the repo cannot be added, without interrupting the install; the installed version is printed afterwards.
- Installing nginx preserves existing configuration: `apt-get install` uses `--force-confold`, so a modified nginx config on the machine is not overwritten (no effect on fresh installs).

### Verification
- Full test suite: 2450 tests, all passing (29 new: 9 for the OCI config CF source, 12 for `[cloudflare]` section parsing and saving, 4 install.sh static tests, 2 DNS credential API tests, 2 credential management page tests).
- Live verification (.160): after deployment the bot's `/domain` works (credentials still come from environment variables, source displayed correctly); the OCI config CF source and section saving were verified through production code paths.
- The nginx.org repository and real IP certificate issuance will be verified when the user runs the install test on .101.

## [1.29.0] - 2026-11-24

### Added
- Automatic Let's Encrypt IP certificate when no domain is given: the installer now requests a certificate for the server's public IP (lego v5.7.0, `shortlived` profile, 160-hour validity), so `https://IP:9527` no longer triggers a browser warning; validation uses http-01 (nginx serves the webroot on port 80); issuance failure falls back to a self-signed certificate without interrupting the install
- Automatic IP certificate renewal: a renewal script plus a cron job every 2 days at 03:00 (in lego v5, `run` also renews; short-lived certs renew by default at half lifetime; a deploy hook reloads nginx after a renewal); the port-80 validation vhost is kept, so renewal never stops nginx
- New `cfdns` validation method for domains: pick `http` (port-80 validation, default, certbot, unchanged) or `cfdns` (Cloudflare DNS-01 via lego; needs a CF API token with Zone:DNS:Edit + Zone:Zone:Read; validation uses no ports; the token lives only in `/etc/lego/env`, mode 600, and is reused for renewal)
- Domain panel falls back to port 9527 when 443 is taken: after a cfdns issuance, if another service (e.g. xray) already holds 443, the panel listens on 9527 instead (the certificate is still valid) and the final message shows the ported URL

### Fixed
- Health check no longer trusts a listening port alone: port 443 may belong to xray, which used to fake a pass; it now requires the nginx process to hold 9527 or 443 (it also used to false-alarm on 9527 for domain deployments)
- UFW now always allows port 80: IP certificate http-01 validation (issuance and renewal) needs it, not only domain installs

### Verification
- Full test suite: 2421 tests, all passing (8 new install.sh static tests: lego version and architectures, IP certificate shortlived/webroot, cfdns prompts and token, renewal automation, IP nginx config, UFW port 80, 443 fallback, health check)
- Real lego v5.7.0 binary verified: `--profile`, `--http.webroot`, `--dns cloudflare`, and `--deploy-hook` all present; account registration against the LE staging server works without `--email` (lego uses `noemail@example.com` as a placeholder); real IP certificate issuance will be verified by the user's install test on .101

## [1.28.1] - 2026-11-24

### Fixed
- OCI config paths in the docs now match v1.28.0: the install script (header notes and post-install message) and README (prerequisites and manual install, both languages) now say `/root/` (config `~/oci_config`, key `/root/xxx.pem`) instead of `~/.oci/`; the legacy path `~/.oci/config` created by `oci setup config` is still read and auto-migrated on the first `/uploadapi` import, now documented as such
- Auth-failure hint now points to `~/oci_config`

### Verification
- Full test suite: 2413 tests, all passing

## [1.28.0] - 2026-11-24

### Added
- OCI config moved to the root home directory: the bot-managed OCI config file is now `~/oci_config` (i.e. `/root/oci_config` when deployed as root), matching R-Bot's placement convention; on first config upload after upgrade, an existing `~/.oci/config` is migrated automatically (old file kept as backup), and CLI calls fall back to the old file until then, so nothing breaks mid-upgrade
- /oci and /uploadapi accept the R-Bot format: an `oci=begin` … `oci=end` block holding multiple `[PROFILE]` sections is imported all at once; content outside the block (username/password/azure, etc.) is ignored
- key_file normalization: absolute paths already under the root home directory are kept as-is (e.g. `/root/1.pem`); everything else becomes `/root/<PROFILE>.pem`; when the original key file already exists on the server it is copied to the new path (mode 600), otherwise the user is told to scp it
- Newly imported accounts become switchable automatically: the import also writes `MBOT_COMPARTMENT_<PROFILE>` (value: the profile's tenancy OCID, i.e. the root compartment) into the env file, so after a bot restart the new account appears in the switcher; the account table is now built dynamically (registered by `MBOT_COMPARTMENT_*` env vars, region read from the config file), bot and web in sync
- Display-name disambiguation for same-region accounts: when several accounts share one home region, the first keeps the plain region name ("新加坡") and later ones get a "·profile" suffix (e.g. "新加坡·sg1"), so the switcher stays unambiguous; bot and web in sync
- Central OCI config path resolution (`resolve_oci_config_path` in `oci/client.py`): `MBOT_OCI_CONFIG_FILE` > `OCI_CLI_CONFIG_FILE` > `~/oci_config` > `~/.oci/config`, passed to every CLI call via environment

### Fixed
- `profile_exists_in_config` could not see the `[DEFAULT]` section: configparser treats DEFAULT as a special section, so `has_section("DEFAULT")` always returned False; fixed with a renamed default section

### Verification
- Full test suite: 2413 tests, all passing (including new tests for path resolution, migration, R-Bot format parsing, key normalization and copy, multi-profile import, the dynamic account table, and same-region display-name disambiguation)
- Live verification (on .160): the existing 4 profiles migrated from `~/.oci/config` to `~/oci_config` with contents verified item by item; instance lists work after bot and web restarts; a R-Bot-format config imported two test accounts in one go — confirm page, write, account-switcher variables, and switcher labels ("新加坡·sg1 / 新加坡·sg2") all fine; the test accounts were removed afterwards at the user's request (he confirmed it was a very old example config and he only has 3 accounts), with the remaining profiles verified unchanged item by item

## [1.27.0] - 2026-11-24

### Added
- /domain record management (bot): the record list now has "Add record" and "Delete record" buttons — previously the bot could only view records, not add or delete them
- Add record: pick a type (A/AAAA/CNAME/TXT/MX) → enter the name (www, or @ for the zone apex) → enter the content (validated per type: A must be IPv4, AAAA must be IPv6, CNAME/MX must be a domain name; invalid input can be re-sent) → a confirmation page (zone/type/name/content, TTL auto, proxy off) → create; the record list refreshes automatically afterwards
- Delete record: pick a record → a confirmation page shows the full record (deletion takes effect immediately and cannot be undone) → confirm to delete; the record list refreshes automatically afterwards; records are looked up by index from the bot's stored list for the current zone, and a stale index asks you to refresh
- Mobile navigation drawer (panel): previously the sidebar was hidden entirely with `display: none` on screens ≤900px wide, so pages like DNS management, quotas and task management had no entry point at all on phones; the top bar now has a hamburger button and the sidebar slides in as a left drawer (tap the button to open, tap the backdrop/a nav link or press ESC to close); the desktop sidebar stays permanently visible

### Fixed
- Removed a stale test task left in the local web/data/tasks.json (a fake grab task from an 08:00 test run) that made test_tasks_api_empty_list fail when run on its own; the file is generated at runtime and is not part of the repo

### Verification
- Full test suite: 2357 tests, all passing (including the new record name/content validation, add and delete flows, and mobile drawer rendering and style tests)
- Live verification (on .160, 16/16 passed): the bot-side Cloudflare client really created a test TXT record (mbot-verify-127) and deleted it, with list checks confirming it appeared and disappeared; the panel DNS endpoints did the same for another test record; the overview page carries the hamburger button and drawer backdrop, and the CSS no longer hides the sidebar
- Mobile viewport test (390×844, real browser over a SOCKS proxy, logged in): the drawer starts closed (left -252), opens on hamburger tap (left 0, DNS management visible), and tapping DNS management opens the /dns page with the add-record button present

## [1.26.0] - 2026-11-24

### Added
- Cloudflare credential management (bot + panel, in sync): when /domain finds no credentials it no longer just errors out — it offers "Add credentials manually" and "Extract from server" buttons; the zone list keyboard gains a "Credential management" entry
- Manual add: API Token (recommended, one step) or Global API Key + email (two steps); credentials are validated against the Cloudflare API (list zones) before saving, and never saved when validation fails; the bot tries to delete the user's message containing the secret so no plaintext remains; credential values are never echoed or logged
- Extract from server: pick a running instance with a public IP, and the bot/panel reads only the CF_Key / CF_Email lines from its certificate script (1.sh) over SSH, validates what it found, and saves it only when validation passes
- Credential management page: shows the current credential source (saved in bot/panel vs. server environment variables), auth method and the masked email; credentials can be replaced, and the saved credential file can be deleted (file only, environment variables untouched)
- New module tgbot/cf_config.py: credentials live in tgbot/data/cloudflare.json (0600, atomic writes), file takes precedence over environment variables; clouds/cloudflare.py itself is unchanged (it only accepts environment variables or explicit parameters) — the new module reads the credentials and passes them explicitly to the client constructor
- The panel DNS page gains a credential card: status display, a manual add form (Token / Key+email toggle), extract from server (instance dropdown + extract button), and delete saved credentials; new endpoints GET/POST/DELETE /api/dns/credentials and POST /api/dns/credentials/extract

### Verification
- Full test suite: 2322 tests, all passing (including the new credential file storage/precedence/extraction-parsing/validation, bot add and extract flows, and web credential endpoint tests)
- Live verification: the panel extract endpoint really extracted credentials from the 1.sh on a Singapore production host (.139), validation passed (6 zones) and the credentials were saved; the file config was deleted afterwards to restore the environment-variable source

## [1.25.0] - 2026-11-24

### Added
- Telegram one-tap login link: the verification-code message now also carries a "tap here to log in directly" link (/login?token=...), which logs you in immediately — no need to type the 6-digit code; the link is single-use and expires in 5 minutes, and the 32-byte random token is far harder to guess than a 6-digit code
- New endpoint POST /api/auth/telegram/magic-login: consumes a one-tap login token and creates a session; new store MagicLoginStore in web/auth.py (single-use, expires, revoked together with the code if sending fails); new setting MBOT_PANEL_URL (the panel's public address, used to build the link; falls back to the request Host when unset)
- Dual-channel login codes: when Bark is configured and enabled, the code and the one-tap login link are also pushed over encrypted Bark, so either Telegram or Bark gets you in; a Bark push failure or exception never breaks the login flow
- The code input now uses autocomplete="one-time-code" and the Telegram form no longer sets autocomplete="off": mobile browsers can now suggest one-time codes received by SMS or email above the keyboard, filling the field with one tap (note: codes in Telegram messages are not a source for the OS autofill, so use the one-tap login link for the Telegram flow)

### Verification
- Full test suite: 2237 tests, all passing (including the new one-tap login token store, send-code link, magic-login endpoint and login-page autofill marker tests)

## [1.24.0] - 2026-11-24

### Added
- Instance CPU model display: the list card and detail modal add a "CPU model" row under Shape (e.g. AMD EPYC 7J13, AMD EPYC 7742, Ampere Altra), and the bot's instance detail shows the same line; the OCI API does not report the host CPU model, so it is read over SSH from the instance's /proc/cpuinfo (lscpu fallback on A1)
- New module tgbot/cpu_model.py: model parsing (short model name), persistent cache (tgbot/data/cpu_models.json — a model never changes for an instance, so it is fetched once and never re-read over SSH), cache entry removed automatically when the instance is terminated
- Background detection on list load: after the list loads, running instances without a cached model are detected in the background and show up on the next refresh; the detail modal adds a "🔄 Re-detect CPU" button (forced re-detection), endpoint POST /api/instances/{id}/cpu-model

### Verification
- Full test suite: 2223 tests, all passing (including the new CPU model parsing/cache/fetch, Web API and detail-text tests)

## [1.23.0] - 2026-11-24

### Added
- New login page based on the open-source Uiverse design (Form by iZOXVL, MIT license): blue-to-purple gradient card with underline floating labels, recreated in plain CSS without Tailwind; password login, Telegram code login, captcha, lockout countdown and Clarity analytics all kept
- Remember me: checking it on the login page extends both the server-side session and the cookie to 30 days (default stays 12 hours); configurable via REMEMBER_ME_DAYS (web/config.py)
- Forgot password entry: clicking it explains how to reset the panel password with the Telegram bot's /raninfo command (no self-service reset exists)

### Changed
- Login page drops the old two-column layout and terminal decoration in favor of a single centered card; functional copy adapted to the panel (username login, default-credentials hint kept); tighter card padding on mobile

### Verification
- Full test suite: 2185 tests, all passing (including the new remember-me cookie/session-expiry and login-page design tests)

## [1.22.0] - 2026-11-24

### Added
- CPU choice on the boot confirmation page (E2 shape, above "Start grabbing"): "Only 7J13" / "Any CPU is fine", the current choice marked with ✅, tapping one refreshes the summary in place; the summary's target-CPU line now always shows for E2 (previously only when 7J13 was selected); A1 does not filter by CPU, so no row there

### Changed
- The task list (menu 21) now says what machine is being grabbed (shape + region + instance name), and the cancel buttons do the same; internal IDs and English type labels like g1 (grab, running, …) are no longer shown; the grab-started message shows the machine description instead of just a task ID; the web task page's first column shows the same description
- Terminated instances are now hidden from the instance list entirely (bot side; the web panel already hid them); the header total counts only visible instances; when everything is terminated it says there is nothing to show (terminated instances are hidden)

### Fixed
- Guard re-enable wording: when the new script is already installed and only the cron job was removed, re-enabling now reports action "enabled" instead of "upgraded", so the bot and the web panel say "re-enabled" instead of wrongly claiming an upgrade to the auto-detect version (found during the .121 live verification)

### Verification
- Full test suite: 2181 tests, all passing (including the new CPU-choice / task-label / hide-terminated / guard re-enable wording tests)

## [1.21.0] - 2026-11-24

### Added
- Traffic-guard toggle in menu 19 (monthly traffic), bot + web: per-host on/off; enabling installs the guard script + a cron job (check every 10 minutes) + runs a test push; disabling removes the cron job and resets any trip; each host in menu 19 now shows its guard status (on/off + block/rate-limit mode)
- Guard template script scripts/oracle_traffic_guard.py: SSH port auto-detection (live sshd port > sshd_config > 22), no more hard-coded port; Bark keys are no longer hard-coded into the script, they are read from /root/.traffic_guard_bark (600) or the BARK_KEY / BARK_ENC_KEY / BARK_SERVER environment variables; on a host with the old script installed, enabling extracts the old keys and keeps using them (falls back to the bot's Bark settings only when no complete key can be extracted)
- Web instance detail adds a "Traffic guard" row with a toggle (GET / POST /api/instances/{id}/watchdog)

### Verification
- Full test suite: 2153 tests, all passing (including the new template-script, install-orchestration, bot-callback and web-API tests); probe detection verified on a Seoul test instance (correctly reported "not installed"); live verification on .121: post-upgrade state (new script + Bark config + cron) in place, disable (cron removed), web re-enable, manual check, and the scheduled 15:00 check updating state all passed (trip was not tested on a production host)

## [1.20.0] - 2026-11-24

### Added
- One-click installer install.sh (user request, 2026-11-24, adapted from the R-Bot template): Debian / Ubuntu, run as root, one command installs the panel + bot + nginx reverse proxy + UFW; first run installs, later runs update and restart; management subcommands start / stop / restart / status / log / upgrade / uninstall
- Two ways to configure the install: interactive prompts (token, user ID, panel password, domain) or non-interactive MBOT_INSTALL_TOKEN / MBOT_INSTALL_USER_ID / MBOT_INSTALL_PASSWORD / MBOT_INSTALL_DOMAIN; an empty password auto-generates a 16-character random one, shown once and saved to /root/.mbot-admin-password (600)
- A domain gets a Let's Encrypt certificate automatically (certbot --nginx); on failure it falls back to a self-signed certificate (panel on https://IP:9527), and the printed panel URL uses the IP, not the domain, after a fallback
- The panel binds only a loopback internal port (127.0.0.1:18001) and nginx proxies to it from outside (443 with a domain, 9527 ssl without); the internal and external ports must differ or uvicorn cannot bind
- UFW allows the current SSH port before being enabled (live sshd port > sshd_config > 22), so it never locks you out; 9527 is always allowed (the certificate fallback needs it too)
- Removes the Oracle cloud image's built-in iptables REJECT rule: it sits ahead of the UFW chains and blocks the panel port (No route to host from outside, fine locally); once UFW is active the rule is redundant, so it is removed from the live chain and from /etc/iptables/rules.v4 (backed up first)
- apt lock waiting + retries: a fresh system runs unattended-upgrades on first boot, holding the apt lock and killing a plain apt-get; waits for the lock (up to 10 minutes) and retries update / install 3 times each
- README quick start is now the one-click install (Chinese + English); the old manual steps stay as \"Manual install\"

### Fixed
- Under nohup / non-interactive runs stdin is EOF, so read returned non-zero and set -e killed the script: every read now ends with || true, and empty input counts as \"not provided\" (found on the Seoul test machine)
- Password hash generation failed because python -c could not import web.auth (the installer's working directory is not necessarily the install directory): PYTHONPATH now points at the install directory, and stderr is shown on failure (found on the Seoul test machine)

### Verified
- Full verification on a Seoul A1 test machine: fresh install (self-signed) → external login page 200 → login with the auto-generated password → update re-run → uninstall and reinstall → domain certificate failure falling back to self-signed → services auto-start after reboot and the panel stays reachable; the bot with a dummy token correctly reports InvalidToken

## [1.19.0] - 2026-11-24

### Added
- Menu 25 (last 3 months spend) now queries all accounts by default (user request, 2026-11-24): per-account totals plus a grand total; a failed account shows "query failed" without affecting the others
- Cloud accounts (menu 8) gains "Add admin account" (user request, 2026-11-24): two-step input (username, then email), confirm, then creates the IAM user and adds it to the Administrators group; oci/iam.py gains create_user / get_group_id_by_name / add_user_to_group
- Microsoft Clarity on the login page (user request, 2026-11-24): the project ID comes from the MBOT_CLARITY_ID environment variable, nothing loads when unset, and no ID is hard-coded in the public repo; login page only for now

### Fixed
- Zero-amount usage items sometimes carry a single space as currency (" " is truthy), which overwrote the real SGD and hid the currency on spend lines: a new _usage_currency helper strips before picking, shared by all three spend formatters (found on real data, 2026-11-24, San Jose account)

## [1.18.0] - 2026-11-24

### Added
- Boot volume backup from the instance detail (user request, 2026-11-24): backup and restore sit in one row, backup first (Telegram bot and web panel in sync); tapping backup creates a FULL backup of the instance's current boot volume
- Delete-before-create when backups are full (user request, 2026-11-24): on a free account with 5 backups, tapping backup on an instance no longer auto-deletes the oldest backup — it prompts "backups are full (5/5), delete an old one first" and offers the delete-history entry; the web panel returns 409 and opens the delete modal (the auto-delete-oldest behaviour on the backups page / volumes page is unchanged)
- Delete historical backups from the instance detail (user request, 2026-11-24): lists the backups of this instance's current boot volume with multi-select delete (checkbox keyboard + confirm on the bot, checkboxes + batch delete on the web)
- Account/region switching edits in place (user request, 2026-11-24): the account list and the region sub-menu now edit the same message instead of popping a new one; returning to the main menu after a switch works the same way; falls back to a new message if the edit fails
- Oracle menu shows 22 items per page (user request, 2026-11-24: two more rows per page): the first page runs to #23, 42 items in 2 pages

## [1.17.2] - 2026-11-24

### Fixed
- Branding sweep (user feedback, 2026-11-24: no R-Bot clone wording in the repo): removed all "R-Bot / clone" wording across the repo — "R-Bot style/shape" in comments and docstrings became panel shape / M_Bot style, the README disclaimer was rewritten (no longer names the original tool), the deploy.sh header and systemd unit descriptions became M_Bot; internal identifiers renamed too: env var prefix RBOT_ → MBOT_ (the deploy host's env file was updated in sync, old names no longer read), JS namespace window.RBot → window.MBot, session cookie rbot_session → mbot_session, MCP server name and default SMTP description became mbot, temp-file and localStorage key prefixes became mbot. The deploy-side service names and paths (rbot-clone-web/bot, /opt/rbot-clone, /root/.rbot-clone.env) keep the old names — renaming them means migrating the deployment, scheduled separately

## [1.17.1] - 2026-11-24

### Fixed
- One-click boot failed with CannotParseRequest on the San Jose / Chicago accounts (user report, 2026-11-24): those accounts had no region plan, so `get_deploy_defaults` silently fell back to the Singapore deploy values (Singapore availability domain / subnet / image) while the CLI pointed at the account's own region, and OCI rejected the cross-region request; the boot form now validates region consistency (the availability domain's region must match the working region, otherwise it errors and asks to configure the region or switch to a configured account/region), enforced at one-click boot, preset save, preset load and post-confirm launch; the web create endpoint got the same check. Region plans for San Jose (ACCOUNT2) and Chicago (ACCOUNT3) were also added (availability domain / subnet / Ubuntu 24.04 and OL 9.8 images for both architectures; the plans live only on the deploy host, never in the repo) and probe-verified: San Jose actually launched an E2 (deleted right away), Chicago returned LimitExceeded — both requests parsed fine
- Web create always failed with the default 47 GB disk: OCI requires at least 50 GB when the boot volume size is specified explicitly, 47 is only the image default; the create form now sends null for 47 or empty (image default 47 GB), rejects 48/49 with a clear error, and accepts 50–200 for explicit sizes; a note was added under the field

## [1.17.0] - 2026-11-24

### Features
- Restore a boot volume onto an instance (user request, 2026-11-24: add boot-volume restore to an instance; semantics confirmed by the user: replace this instance's boot volume, stop the instance, swap the disk, restore in place): the instance detail page gains a "💾 引导卷恢复" button (shown only for RUNNING / STOPPED instances); it lists the AVAILABLE backups of the instance's current boot volume, and after picking one and confirming, a background task runs: create a new boot volume from the backup (same availability domain as the instance) → replace the boot volume through OCI's native replace API (`update-instance-source-via-boot-volume-details`, OCI stops the instance, swaps the disk and restores the previous state itself; a running instance comes back RUNNING, a stopped one stays STOPPED); on failure the code re-checks the attachment and instance state to confirm the rollback: if the old volume is still attached and the state is back, OCI rolled back automatically and the unattached new volume is deleted; if the new volume is already attached it is never deleted and the message asks for a manual check; the old volume is only detached, never deleted — it still counts toward the boot-volume quota, delete it manually from the Volumes page once the new disk is confirmed good; the boot-volume quota (200 GB free tier) is pre-checked before creating the new volume, and the restore is refused without touching the instance when there is not enough room; only one swap per instance at a time. Progress and results go out over Telegram + Bark. Implementation note: manual detach + attach does not work — a boot volume restored from a backup is rejected by attach (Conflict, it can only be attached back to its original instance), so the native replace API is required; verified on a real machine in Seoul
- Web panel in sync: the instance detail dialog gains a "引导卷恢复" button (only for RUNNING / STOPPED instances); it opens a backup picker, and confirming starts a background swap task and jumps to the Tasks page for progress; new endpoints GET /api/instances/{id}/boot-backups (the instance's boot volume's available backups) and POST /api/instances/{id}/restore-boot-volume (body {backup_id, confirm}, confirm must be true; returns 409 when a swap is already running for the instance)

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
- Auto-fill network fields on the create form (user feedback, 2026-11-24: nobody knows how to fill in the availability domain / subnet — they should be selected automatically): new GET /api/create/defaults endpoint returns the availability domain and subnet for the selected account's region from the deployment config (same get_deploy_defaults as the bot's boot form: MBOT_PLAN_* env vars are the home-region defaults, region_plans.json wins when that region is configured). The form fills them on page load and when switching accounts; they stay editable, and empty when unconfigured — also left empty when the selected account's region differs from the region the deployment defaults belong to, so a home-region subnet is never filled into another account and fails the launch
- SSH key file is now optional (user feedback, 2026-11-24: fill it in when a key has been uploaded, leave it blank when there is none): the path is prefilled when the key file exists on the panel server, left blank otherwise; POST /api/create no longer requires ssh_keys_file — when blank the grabber omits --ssh-authorized-keys-file and the instance relies on root password login (on by default)

## [1.12.0] - 2026-11-24

### Features
- Auto-generate Bark encryption key (user request, 2026-11-24): menu 43 and the panel "Bark notifications" page gain a "🎲 Auto-generate Key" button that creates a random 32-char key (16 random bytes, hex-encoded — equivalent to `openssl rand -hex 16`; note `openssl rand -hex 32` produces 64 chars, too long to use as a key). A confirmation page comes first (the old key stops working immediately after generating); the new key is shown only once, with a QR code the Bark app can scan to add it (or copy it manually). The bot sends the QR as a photo, the web page shows it in the dialog; without segno installed there is no QR code, just the key text

## [1.11.0] - 2026-11-24

### Features
- Default root password for new instances is now a UUID (user request, 2026-11-24): the grabber generates a UUID4 32-hex-char password by default; the confirmation page shows it (the bot does not remember it — the message says so), and "Start grabbing" reuses the same one. Web panel create page in sync: root login toggle on by default, password field prefilled with a random UUID (regenerable), shown once in a dialog after submit
- Boot success notification reformatted as the success message (user screenshot feedback, 2026-11-24): title "🎉 恭喜，开机成功！", fields in the order account / CPU type / CPU count / memory / disk / root password / SSH key / launch count (i/N) / elapsed time / grab attempts / region (region code + Chinese name) / IP, with a "注意📢：root 密码机器人不会记忆" note at the end; web-panel launches now also send dual-channel notifications (Telegram + Bark, Telegram only if Bark is not configured), with an extra pinned alert for paid shapes
- Bark settings completed (user feedback, 2026-11-24: Bark settings were nowhere to be found in bot or web): bot gains a top-level /bark command jumping straight to the settings page (menu 43 still exists); panel gains a "Bark notifications" page (address + 32-char key + test push + switch), sharing tgbot/data/bark.json with the bot; command menu grows from 20 to 21 commands
- Account list shows the actual subscribed-region count (user feedback, 2026-11-24): the switch-account list uses the "新加坡、3 个区" format, with the count computed from real region subscriptions; single-region accounts show no suffix

### Fixes
- Grabber dedup mistook a terminated instance for a success (real bug, found by a live test in Seoul, 2026-11-24): a same-name TERMINATED instance inside the 15-minute window was returned as "previously created successfully"; only RUNNING / PROVISIONING / STARTING instances are now accepted
- Switching account/region now returns straight to the main menu (user feedback, 2026-11-24): on success the current message is edited back into the /oracle main menu (with a confirmation line), and the intermediate account/region list messages are deleted instead of piling up; duplicate clicks within 5 seconds are deduplicated, so no more double confirmations
- Deleted boot volumes are fully hidden (user request, 2026-11-24, supersedes the v1.8.1 group/collapse approach): bot menu 7 and the web Volumes page both filter out TERMINATED / TERMINATING volumes; the deleted-volumes section is removed
- Branding sweep (user screenshot feedback, 2026-11-24): login page title and logo letter R changed to M_Bot / M, top-bar logo letter R changed to M, /start welcome text "我是 M_Bot 机器人" changed to M-探长; wording in code comments and the JS internal namespace is implementation detail invisible to users, kept as-is
- README disclaimer updated to the user-supplied wording (bilingual); new CHANGELOG.en.md English changelog, updated bilingually from now on

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
- Bark notifications (new module tgbot/bark.py + menu 43 "Bark 通知设置"): AES-256-CBC encrypted push (same spec as send_bark.py, cryptography encryption with an openssl CLI fallback, byte-identical); set the Bark address, 32-char encryption key, test push (independent of the switch), and switch in the menu; config stored in tgbot/data/bark.json (0600), address shown masked, key never echoed; MBOT_BARK_URL / MBOT_BARK_KEY env vars can serve as defaults
- Boot success dual-channel push (Telegram + Bark): sent automatically after a successful grab, formatted as the success message (account, region, CPU type/count, memory, disk, grab attempts, elapsed time, public IP; password line only when a root password was set); Telegram is called directly over HTTP (works from daemon threads); public IP lookup retries 3 times, then says "获取中"; notification failure is only logged and never breaks the grab task
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
  - The guard uses a dedicated SSH key (env var MBOT_GUARD_SSH_KEY, generated at deploy time with its public key added to each instance)

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
- Boot volume backup & restore (M_Bot exclusive): panel boot volumes page + bot menu 7, both sides
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
- Instances page redesigned as cards : each card shows name + status badge, public IP, shape, specs, boot volume (size · VPU + status badge + attached badge), creation time; button matrix: SSH, power off/on/restart, serial console, rename, resize (A1 only), repair, reset image, IP management, boot volume, delete, delete (keep boot volume)
- IP management dialog: change IP / attach IPv6 / delete IPv6 in one place
- Boot volume dialog: size / VPU / state / attachment state / OCID, with online resize (grow only)
- Delete (keep boot volume): terminates the instance but keeps the boot volume, deletable later from the volumes page; the confirmation states it occupies the 200GB free quota

### Fixes
- Switching profiles now clears the list before loading, fixing the one-frame flash of old data from v1.3.0

## [1.5.0] - 2026-11-24

### Features
- Telegram command menu: the bot registers 20 commands in Telegram's menu at startup (/start /help /oracle /instances /domain /azure /aws /gcp /do /vf /solusvm /oci /command /pubkey /uploadapi /oproxy /clearlock /raninfo /me /admin), following the assistant menu wording
- /me: my info (Telegram ID, username, panel version, RUNNING instance count per account)
- /admin: admin menu (panel status, restart panel/bot services, view version; restart needs a second confirmation)
- /oci: upload an Oracle or Azure config file (appended as a profile after validation; the private key never goes through Telegram — scp it yourself)
- /clearlock: clear a panel login-failure lock (restarts the panel service; lock state lives in the panel process memory)
- /raninfo: reset the panel admin password to a random one (pbkdf2-SHA256 hash written to env, plaintext to a 600 password file, panel restarted; the new password is sent privately once)
- Six cloud commands (/azure /aws /gcp /do /vf /solusvm): reply with setup instructions when unconfigured, and say plainly the feature isn't implemented when configured but the module is a stub — never fabricate data

## [1.4.0] - 2026-11-24

### Features
- Instance operations completed : detail dialog (public/private IP, IPv6 address, boot volume size), change IP, attach/delete IPv6, A1 resize, one-click repair
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
- Instances / boot volumes / quota / create pages gain a dynamic account dropdown ("name · region Chinese name"); switching reloads with ?profile=, localStorage remembers the last choice; no standalone region selector (region is an attribute of the account)
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
- Web panel branding unified to M_Bot: all page titles, top-bar logo, login page, and the Telegram verification SMS signature changed to M_Bot Oracle开源管理面板

## [1.2.0] - 2026-11-24

### Features
- Menu 1 (boot) becomes a button-matrix create form: CPU type / OS / OCPU / memory / disk / count / delay, each row tap-to-select (tap again to clear); picking two delays gives a random interval
- Batch creation: up to 4 at once, quota pre-check computed as count × spec; root login with a random password (shown once in the launch message); public IP / IPv6 toggles; startup script via file upload or pasted text
- Forms can be saved as quick-boot presets (menu 17 one-click boot); CentOS maps to Oracle Linux (requires MBOT_IMAGE_OL_* images)
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
