#!/usr/bin/env python3
"""Web 面板入口（FastAPI）。

启动方式（三选一）：
    uvicorn web.app:app --host 127.0.0.1 --port 9527
    python3 -m web.app
    uvicorn --factory web.app:create_app --host 127.0.0.1 --port 9527

启动前必须设置管理员密码哈希（否则拒绝启动）：
    python3 -m web.auth          # 交互生成哈希
    export MBOT_WEB_PASSWORD_HASH='<生成的哈希>'

WebSocket 路由（Web SSH 终端）在 web/ssh.py 里，由另一个子代理负责；
导入失败时跳过并打印警告，不让面板崩掉。
"""

import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

from web import config
from web import demo_data
from web.auth import SESSIONS
from web.routes import auth as auth_routes
from web.routes import batch as batch_routes
from web.routes import boot_backups as boot_backups_routes
from web.routes import bark as bark_routes
from web.routes import cert as cert_routes
from web.routes import cloudreport as cloudreport_routes
from web.routes import create as create_routes
from web.routes import bind_domain as bind_domain_routes
from web.routes import dns as dns_routes
from web.routes import email as email_routes
from web.routes import favorites as favorites_routes
from web.routes import gfw as gfw_routes
from web.routes import hosts as hosts_routes
from web.routes import inspect as inspect_routes
from web.routes import instances as instances_routes
from web.routes import monitor as monitor_routes
from web.routes import objectstorage as objectstorage_routes
from web.routes import pages as pages_routes
from web.routes import plans as plans_routes
from web.routes import profiles as profiles_routes
from web.routes import quota as quota_routes
from web.routes import rescue as rescue_routes
from web.routes import settings as settings_routes
from web.routes import system as system_routes
from web.routes import volumes as volumes_routes


class NoCacheStaticFiles(StaticFiles):
    # 静态文件不缓存：每次加载都重新校验，浏览器不按启发式缓存旧 JS/CSS。
    # Static files must not be cached heuristically: force revalidation on every
    # load, otherwise Safari keeps serving a stale bundle after a release
    # (v1.45.0 cloud-report table stayed invisible until this header was added).
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def _lifespan(app):
    """应用生命周期：启动时按需启动抢机计划与巡检调度器，关闭时停掉。

    调度器默认关闭（config.ENABLE_PLAN_SCHEDULER，环境变量
    MBOT_ENABLE_PLAN_SCHEDULER=1 才启动；巡检调度器同理，
    环境变量 MBOT_ENABLE_INSPECT_SCHEDULER=1），测试不受影响。
    """
    # Startup recovery: grab tasks from this source that are still running resume from their grab_spec; other unfinished tasks are marked interrupted (bot-side tasks are untouched). 启动恢复：本来源还在跑的抢机任务按 grab_spec 恢复重跑，其余未结束任务标为已中断（机器人端的任务不动）。
    # Demo mode has no real tasks (the task list is simulated), so skip the recovery entirely. 演示模式没有真实任务（任务列表是模拟的），跳过启动恢复。
    from web.tasks import TASKS, resume_interrupted_grabs

    # Unified JSON config: (re)load it at startup so a config change
    # made while the panel was down is picked up, and regenerate the
    # OCI INI from JSON (the CLI and OCIClient still read the INI).
    # Idempotent; web/config.py already bootstrapped once at import.
    # 统一 JSON 配置：启动时再加载一次（面板停机期间改的配置能生效），
    # 并从 JSON 重生成 OCI INI（CLI 与 OCIClient 仍读 INI）。幂等；
    # web/config.py 在 import 时已引导过一次。
    import mbot_config

    mbot_config.bootstrap()

    if not config.DEMO_MODE:
        resume_interrupted_grabs(TASKS)
        # 第 6 轮 CPU 优化：启动 OCI 数据缓存预热线程。只保暖、不点火——
        # 缓存里没有的条目不预热，面板闲置时不产生后台 OCI 调用。
        from web.oci_cache import start_cache_prewarm

        start_cache_prewarm()
    if config.ENABLE_PLAN_SCHEDULER:
        from web.tasks import start_plan_scheduler

        start_plan_scheduler()
    if inspect_routes.ENABLE_INSPECT_SCHEDULER:
        inspect_routes.start_inspect_scheduler()
    yield
    from web.tasks import stop_plan_scheduler

    stop_plan_scheduler()
    inspect_routes.stop_inspect_scheduler()
    if not config.DEMO_MODE:
        from web.oci_cache import stop_cache_prewarm

        stop_cache_prewarm()


def _response_sets_session_cookie(response):
    """响应是否已设置会话 cookie。

    登录接口自己设了 cookie 时，演示自动登录中间件不再覆盖它
    （登录成功时服务端会话与 cookie 都以登录接口建的为准）。
    """
    name = (config.SESSION_COOKIE + "=").encode("ascii")
    for raw_name, raw_value in response.raw_headers:
        if raw_name.lower() == b"set-cookie" and name in raw_value:
            return True
    return False


def create_app():
    """FastAPI 应用工厂。

    ADMIN_PASSWORD_HASH 未设置时抛 RuntimeError 拒绝启动
    （测试时可通过环境变量 MBOT_WEB_PASSWORD_HASH 设一个测试哈希绕过）；
    演示模式（config.DEMO_MODE）除外：demo/demo 可直接登录，不依赖
    真实管理员密码哈希。
    """
    if not config.ADMIN_PASSWORD_HASH and not config.DEMO_MODE:
        raise RuntimeError(
            "未设置管理员密码哈希，拒绝启动。"
            "先运行 python3 -m web.auth 生成哈希，"
            "再设环境变量 MBOT_WEB_PASSWORD_HASH 后启动。")

    # FastAPI auto-docs (/docs, /redoc, /openapi.json) are visible without login by default and would expose every endpoint and parameter shape: disable them (vuln test M-1). FastAPI 自动文档（/docs、/redoc、/openapi.json）默认未登录就能看，会暴露全部接口与参数结构：关掉（漏洞测试 M-1）。
    app = FastAPI(title="M_Bot Web 面板", version="1.2.0",
                  docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=_lifespan)

    # Unified error envelope: HTTPException / validation failures also return {"ok": false, "error": "..."}, matching the manual error responses in each route. 统一错误信封：HTTPException / 参数校验失败也返回 {"ok": false, "error": "..."}，与各路由的手动错误响应一致。
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code,
                            content={"ok": False, "error": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request,
                                           exc: RequestValidationError):
        # Only field-level errors are returned: str(exc) carries server file paths and line numbers (e.g. web/routes/auth.py line 164) and must never be returned to clients as-is. 只返回字段级错误：str(exc) 会带服务器文件路径与行号（如 web/routes/auth.py line 164），不能原样返回给客户端。
        try:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err.get('loc', []))}: "
                f"{err.get('msg', '')}"
                for err in exc.errors()
            )
        except Exception:
            details = ""
        if not details:
            details = "请求参数错误"
        return JSONResponse(status_code=422,
                            content={"ok": False,
                                     "error": f"请求参数错误：{details}"})

    # Static files (owned by the frontend subagent; skip the mount when the directory does not exist, without erroring). 静态文件（前端子代理负责；目录不存在时跳过挂载，不报错）。
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "static")
    if os.path.isdir(static_dir):
        app.mount("/static", NoCacheStaticFiles(directory=static_dir),
                  name="static")
    else:
        print(f"警告：静态文件目录不存在，跳过 /static 挂载：{static_dir}")

    # Demo mode: no login required — visitors enter the panel directly.
    # Requests without a valid session get a demo session auto-created
    # (same SessionStore / cookie mechanism as a normal demo/demo login),
    # so page routes and API routes keep working unchanged; the demo
    # login page itself redirects to /overview (see pages.login_page).
    # Static files are skipped (they need no session).
    # 演示模式：不用登录直接进面板——无有效会话的请求自动按 demo 用户
    # 建会话（与正常 demo/demo 登录同一套 SessionStore / cookie 机制），
    # 页面路由与 API 路由无需改动；静态文件跳过（不需要会话）。
    # 非演示模式完全不受影响。
    @app.middleware("http")
    async def _demo_auto_login_middleware(request, call_next):
        auto_token = None
        if (config.DEMO_MODE
                and not request.url.path.startswith("/static")):
            token = request.cookies.get(config.SESSION_COOKIE)
            if not SESSIONS.get(token):
                auto_token = SESSIONS.create(demo_data.DEMO_USERNAME)
                # Make the new session visible to this same request:
                # rewrite the Cookie header so get_current_user /
                # _require_login see the demo token right away
                # (otherwise the first request would still 401 and only
                # the second one would work). 让本次请求立刻看到新会
                # 话：改写 Cookie 头，否则第一次请求仍会 401、第二次
                # 才生效。
                headers = MutableHeaders(scope=request.scope)
                cookies = dict(request.cookies)
                cookies[config.SESSION_COOKIE] = auto_token
                headers["cookie"] = "; ".join(
                    f"{name}={value}" for name, value in cookies.items())
        response = await call_next(request)
        if (auto_token is not None
                and not _response_sets_session_cookie(response)):
            response.set_cookie(
                config.SESSION_COOKIE, auto_token,
                httponly=True,
                secure=auth_routes._is_https_request(request),
                samesite="lax", path="/",
                max_age=int(config.SESSION_EXPIRE_HOURS * 3600))
        return response

    app.include_router(auth_routes.router)
    app.include_router(system_routes.router)
    app.include_router(instances_routes.router)
    app.include_router(volumes_routes.router)
    app.include_router(boot_backups_routes.router)
    app.include_router(rescue_routes.router)
    app.include_router(gfw_routes.router)
    app.include_router(bark_routes.router)
    app.include_router(quota_routes.router)
    app.include_router(dns_routes.router)
    app.include_router(bind_domain_routes.router)
    app.include_router(create_routes.router)
    app.include_router(hosts_routes.router)
    app.include_router(objectstorage_routes.router)
    app.include_router(email_routes.router)
    app.include_router(plans_routes.router)
    app.include_router(profiles_routes.router)
    app.include_router(batch_routes.router)
    app.include_router(favorites_routes.router)
    app.include_router(cert_routes.router)
    app.include_router(inspect_routes.router)
    app.include_router(cloudreport_routes.router)
    app.include_router(monitor_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(pages_routes.router)

    # WebSocket routes (web SSH terminal, written by another subagent): WebSocket 路由（Web SSH 终端，另一个子代理在写）：
    # on import failure, skip and print a warning instead of crashing the panel. 导入失败时跳过并打印警告，不要让面板崩掉。
    try:
        from web.ssh import router as ssh_router
        app.include_router(ssh_router)
    except ImportError as exc:
        print(f"警告：Web SSH 模块未就绪，跳过 WebSocket 路由（{exc}）")

    # SFTP file-panel routes: same fault-tolerant approach as the ssh router. SFTP 文件面板路由：与 ssh router 同样的容错方式。
    try:
        from web.sftp import router as sftp_router
        app.include_router(sftp_router)
    except ImportError as exc:
        print(f"警告：SFTP 模块未就绪，跳过 SFTP 路由（{exc}）")

    return app


# Module-level app instance, for `uvicorn web.app:app`. 模块级 app 实例，供 `uvicorn web.app:app` 使用。
# Not created when the password hash is unset (create_app raises RuntimeError; set MBOT_WEB_PASSWORD_HASH first per the module docstring, then start). Tests importing this module have no hash set, so no creation is triggered and tests are unaffected. 密码哈希未设置时不创建（create_app 会抛 RuntimeError；此时按模块 docstring 先设 MBOT_WEB_PASSWORD_HASH 再启动）。测试 import 本模块时哈希未设置，不会触发创建，不影响测试。
# Demo mode is the exception: it starts without a hash (demo/demo logs in). 演示模式例外：无哈希也可启动（demo/demo 可登录）。
if config.ADMIN_PASSWORD_HASH or config.DEMO_MODE:
    app = create_app()


if __name__ == "__main__":
    import uvicorn

    try:
        _app = create_app()
    except RuntimeError as exc:
        print(f"启动失败：{exc}")
        sys.exit(1)
    uvicorn.run(_app, host=config.HOST, port=config.PORT)
