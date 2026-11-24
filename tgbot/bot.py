#!/usr/bin/env python3
"""Telegram 机器人入口（R-Bot 克隆阶段 2）。

用法：
    export RBOT_TELEGRAM_TOKEN="<BotFather 给的 Token>"
    export RBOT_ALLOWED_USERS="<你的 Telegram 用户 ID>"
    python3 -m tgbot.bot

机器人用 polling 方式收消息（不需要公网端口、不需要域名）。
白名单是硬规则：不在 RBOT_ALLOWED_USERS 里的用户发任何命令都直接拒绝。
"""

import datetime
import logging
import os
import sys
from zoneinfo import ZoneInfo

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

# 把项目根目录加到 sys.path 开头（保证 `import oci` 解析到本项目
# 的 oci/ 包，而不是系统里可能装的 OCI Python SDK 同名包）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config  # noqa: E402
from tgbot import handlers, handlers_client, handlers_extra  # noqa: E402
from tgbot import handlers_menu  # noqa: E402
from tgbot.services import run_sync  # noqa: E402

# ================= 配置区（改这里） =================
# 实例状态监控间隔（分钟）：check_instances_job 每隔这么久检查一次。
MONITOR_INTERVAL_MINUTES = 10
# 每日报告发送时间（24 小时制，机器人所在机器的本地时区）。
DAILY_REPORT_TIME = datetime.time(hour=8, minute=0)
# SSL 证书到期检查时间（每天一次，机器人所在机器的本地时区）。
SSL_CHECK_TIME = datetime.time(hour=8, minute=30)
# 账单预警检查时间（每天一次，北京时间早上 8 点；带时区，
# 不受机器人所在机器时区影响）。
BILLING_CHECK_TIME = datetime.time(hour=8, minute=0,
                                   tzinfo=ZoneInfo("Asia/Shanghai"))
# 日志级别。
LOG_LEVEL = "INFO"
# =====================================================


def _setup_logging():
    """日志同时写文件（供"查询最新日志"命令读）与控制台。"""
    log_file = bot_config.LOG_FILE
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # httpx / telegram 的 DEBUG 日志太多，压到 WARNING。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


async def _post_init(application):
    """启动后注册命令菜单（Telegram 客户端"菜单"按钮显示全部命令）。

    菜单内容见 tgbot/handlers_menu.py 的 BOT_COMMANDS；测试保证
    菜单列表与已注册的 CommandHandler 一致。同时后台预热订阅区域
    缓存（菜单顶行与切换菜单的“（N 个区）”后缀读缓存，预热后首次
    打开菜单就能带区数；失败只记日志）。
    """
    await application.bot.set_my_commands(
        [BotCommand(command, description)
         for command, description in handlers_menu.BOT_COMMANDS])
    logging.getLogger(__name__).info(
        "已注册 %d 个机器人命令到 Telegram 菜单",
        len(handlers_menu.BOT_COMMANDS))

    async def _warm_region_cache():
        try:
            await run_sync(handlers_extra.get_profile_regions)
        except Exception as exc:  # noqa: BLE001 - 预热失败不影响启动
            logging.getLogger(__name__).warning(
                "预热订阅区域缓存失败：%s", exc)

    application.create_task(_warm_region_cache())


def build_application():
    """构建 Application 并注册全部命令/回调/定时任务。返回 Application。"""
    token = bot_config.get_token()  # 未配置时抛 RuntimeError，启动即失败
    if not bot_config.ALLOWED_USER_IDS:
        raise RuntimeError(
            "未配置白名单：请设置环境变量 RBOT_ALLOWED_USERS（逗号分隔的 "
            "Telegram 用户 ID），否则谁都无法使用机器人。")

    application = (Application.builder().token(token)
                   .post_init(_post_init).build())

    # ---- 命令 ----
    application.add_handler(CommandHandler("start", handlers.start_command))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("oracle", handlers.oracle_command))
    application.add_handler(CommandHandler("instances",
                                           handlers.instances_command))
    application.add_handler(CommandHandler("bark",
                                           handlers_extra.bark_command))
    application.add_handler(CommandHandler("domain",
                                           handlers_extra.handle_domain))
    # ---- 客户端工具命令（阶段 5 工作包 B） ----
    application.add_handler(CommandHandler("command",
                                           handlers_client.command_command))
    application.add_handler(CommandHandler("pubkey",
                                           handlers_client.pubkey_command))
    application.add_handler(CommandHandler("oproxy",
                                           handlers_client.oproxy_command))
    application.add_handler(CommandHandler("uploadapi",
                                           handlers_client.uploadapi_command))
    # ---- 其他云命令（handlers_menu） ----
    application.add_handler(CommandHandler("azure",
                                           handlers_menu.azure_command))
    application.add_handler(CommandHandler("aws",
                                           handlers_menu.aws_command))
    application.add_handler(CommandHandler("gcp",
                                           handlers_menu.gcp_command))
    application.add_handler(CommandHandler("do",
                                           handlers_menu.do_command))
    application.add_handler(CommandHandler("vf",
                                           handlers_menu.vf_command))
    application.add_handler(CommandHandler("solusvm",
                                           handlers_menu.solusvm_command))
    # ---- 配置 / 管理命令（handlers_menu） ----
    application.add_handler(CommandHandler("oci",
                                           handlers_menu.oci_command))
    application.add_handler(CommandHandler("clearlock",
                                           handlers_menu.clearlock_command))
    application.add_handler(CommandHandler("raninfo",
                                           handlers_menu.raninfo_command))
    application.add_handler(CommandHandler("me",
                                           handlers_menu.me_command))
    application.add_handler(CommandHandler("admin",
                                           handlers_menu.admin_command))

    # ---- 回调（按 callback_data 前缀路由，见 keyboards.py 的规范） ----
    application.add_handler(CallbackQueryHandler(
        handlers.oracle_menu_callback, pattern=r"^o:"))
    application.add_handler(CallbackQueryHandler(
        handlers.instance_list_callback, pattern=r"^i:list$"))
    application.add_handler(CallbackQueryHandler(
        handlers.noop_callback, pattern=r"^i:noop$"))
    application.add_handler(CallbackQueryHandler(
        handlers.instance_detail_callback, pattern=r"^i:detail:"))
    application.add_handler(CallbackQueryHandler(
        handlers.instance_action_callback,
        pattern=r"^i:(start|stop|restart|terminate|ip|resize|rename|reinstall|console):"))
    application.add_handler(CallbackQueryHandler(
        handlers.confirm_callback, pattern=r"^c:"))
    application.add_handler(CallbackQueryHandler(
        handlers.cancel_callback, pattern=r"^x$"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_preset_callback, pattern=r"^b:boot:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_go_callback, pattern=r"^b:go$"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_preset_load_callback, pattern=r"^b:load:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_form_callback, pattern=r"^bf:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.task_cancel_callback, pattern=r"^t:cancel:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.profile_region_callback, pattern=r"^pr:"))
    # 注意：pr: 必须注册在 p: 之前（"pr:..." 也匹配 ^p:，先注册的先处理）。
    application.add_handler(CallbackQueryHandler(
        handlers_extra.profile_callback, pattern=r"^p:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.quota_callback, pattern=r"^q:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.resize_preset_callback, pattern=r"^r:"))
    application.add_handler(CallbackQueryHandler(
        handlers.noop_callback, pattern=r"^v:noop$"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.volume_detail_callback, pattern=r"^v:detail:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.volume_resize_callback, pattern=r"^v:resize:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.volume_size_callback, pattern=r"^v:volsize:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.volume_delete_callback, pattern=r"^v:delete:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.volume_backup_callback, pattern=r"^v:backup:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_backups_callback, pattern=r"^bb:list$"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_backup_paid_callback, pattern=r"^bb:paid$"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_backup_multiselect_callback, pattern=r"^bb:ms$"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_backup_mstoggle_callback, pattern=r"^bb:mst:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_backup_msdelete_callback, pattern=r"^bb:msdel$"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_backup_mscancel_callback, pattern=r"^bb:mscancel$"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_backup_restore_callback, pattern=r"^bb:restore:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.boot_backup_delete_callback, pattern=r"^bb:delete:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.ipv6_create_callback, pattern=r"^ipv6_create:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.ipv6_delete_callback, pattern=r"^ipv6_delete:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.ip_create_callback, pattern=r"^ip_create$"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.ip_delete_callback, pattern=r"^ip_delete:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.user_callback, pattern=r"^u:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.domain_callback, pattern=r"^domain:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.batch_toggle_callback, pattern=r"^ba:toggle:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.batch_next_callback, pattern=r"^ba:next$"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.batch_action_callback, pattern=r"^ba:action:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.traffic_guard_callback, pattern=r"^g:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.gfw_callback, pattern=r"^gfw:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.keepalive_callback, pattern=r"^k:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.ssl_watch_callback, pattern=r"^s:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.bark_callback, pattern=r"^bark:"))
    # ---- 工作包 D 回调（对象存储 / 抢机计划 / 客户端管理） ----
    application.add_handler(CallbackQueryHandler(
        handlers_extra.objectstorage_callback, pattern=r"^os:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.plan_callback, pattern=r"^pl:"))
    application.add_handler(CallbackQueryHandler(
        handlers_extra.client_callback, pattern=r"^cl:"))
    # ---- 客户端工具回调（阶段 5 工作包 B） ----
    application.add_handler(CallbackQueryHandler(
        handlers_client.command_host_callback, pattern=r"^cmd:"))
    application.add_handler(CallbackQueryHandler(
        handlers_client.pubkey_callback, pattern=r"^pk:"))
    application.add_handler(CallbackQueryHandler(
        handlers_client.oproxy_callback, pattern=r"^px:"))
    application.add_handler(CallbackQueryHandler(
        handlers_client.uploadapi_callback, pattern=r"^ua:"))
    application.add_handler(CallbackQueryHandler(
        handlers_client.memballoon_callback, pattern=r"^mh:"))
    application.add_handler(CallbackQueryHandler(
        handlers_client.memclear_callback, pattern=r"^mc:"))
    # ---- 菜单命令回调（handlers_menu：/oci 确认 / /admin 菜单） ----
    application.add_handler(CallbackQueryHandler(
        handlers_menu.oci_callback, pattern=r"^oci:"))
    application.add_handler(CallbackQueryHandler(
        handlers_menu.admin_callback, pattern=r"^adm:"))

    # ---- 文字输入（改名 / 重装确认，见 handlers_extra.text_input_callback） ----
    # 只处理纯文字消息；命令仍走下面的 unknown_command。
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handlers_extra.text_input_callback))
    # ---- 客户端工具的待输入消息（阶段 5 工作包 B） ----
    # 纯文本按 user_data 里的待输入状态分发（无状态时静默忽略，
    # 与上面的 text_input_callback 共存，各自只处理自己的状态）；
    # 文件只在 /uploadapi 等待上传时处理。
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handlers_client.handle_client_text_input))
    application.add_handler(MessageHandler(
        filters.Document.ALL, handlers_client.handle_client_document))
    # ---- 开机表单的脚本文件上传（菜单 1 表单） ----
    # 只在 pending_input 是 boot_script 时处理，与上面的
    # handle_client_document 共存（各自只认自己的状态）。
    application.add_handler(MessageHandler(
        filters.Document.ALL, handlers_extra.boot_form_document_callback))
    # ---- /oci 的 config 文件上传与粘贴文本 ----
    # 只在 oci_awaiting 状态时处理，与上面的 document/text 处理
    # 共存（各自只认自己的状态；ua_awaiting 优先于 oci_awaiting）。
    application.add_handler(MessageHandler(
        filters.Document.ALL, handlers_menu.handle_oci_document))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handlers_menu.handle_oci_text_input))

    # ---- 未知命令 / 未知消息 ----
    application.add_handler(MessageHandler(filters.COMMAND,
                                           handlers.unknown_command))

    # ---- 错误处理 ----
    application.add_error_handler(handlers.error_handler)

    # ---- 定时任务（开关在 job 函数内部判断，见 handlers_extra） ----
    job_queue = application.job_queue
    if job_queue is not None:
        job_queue.run_repeating(
            handlers_extra.check_instances_job,
            interval=datetime.timedelta(minutes=MONITOR_INTERVAL_MINUTES),
            first=datetime.timedelta(seconds=30),
            name="check_instances")
        job_queue.run_daily(
            handlers_extra.daily_report_job,
            time=DAILY_REPORT_TIME,
            name="daily_report")
        job_queue.run_daily(
            handlers_extra.ssl_check_job,
            time=SSL_CHECK_TIME,
            name="ssl_check")
        job_queue.run_daily(
            handlers_extra.billing_check_job,
            time=BILLING_CHECK_TIME,
            name="billing_check")
        job_queue.run_repeating(
            handlers_extra.gfw_check_job,
            interval=datetime.timedelta(
                minutes=handlers_extra.GFW_CHECK_INTERVAL_MINUTES),
            first=datetime.timedelta(seconds=60),
            name="gfw_check")
    else:
        logging.getLogger(__name__).warning(
            "job_queue 不可用（未安装 python-telegram-bot[job-queue]），"
            "监控、每日报告、SSL 检查、账单预警与 GFW 检查定时任务未注册。")

    return application


def main():
    """启动机器人（polling）。"""
    _setup_logging()
    logger = logging.getLogger(__name__)
    application = build_application()
    logger.info("机器人启动，白名单用户：%s",
                sorted(bot_config.ALLOWED_USER_IDS))
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
