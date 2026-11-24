#!/usr/bin/env python3
"""Web 面板入口（FastAPI）。

启动方式（三选一）：
    uvicorn web.app:app --host 127.0.0.1 --port 9527
    python3 -m web.app
    uvicorn --factory web.app:create_app --host 127.0.0.1 --port 9527

启动前必须设置管理员密码哈希（否则拒绝启动）：
    python3 -m web.auth          # 交互生成哈希
    export RBOT_WEB_PASSWORD_HASH='<生成的哈希>'

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

from web import config
from web.routes import auth as auth_routes
from web.routes import batch as batch_routes
from web.routes import boot_backups as boot_backups_routes
from web.routes import cert as cert_routes
from web.routes import cloudreport as cloudreport_routes
from web.routes import create as create_routes
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
from web.routes import settings as settings_routes
from web.routes import system as system_routes
from web.routes import volumes as volumes_routes


@asynccontextmanager
async def _lifespan(app):
    """应用生命周期：启动时按需启动抢机计划与巡检调度器，关闭时停掉。

    调度器默认关闭（config.ENABLE_PLAN_SCHEDULER，环境变量
    RBOT_ENABLE_PLAN_SCHEDULER=1 才启动；巡检调度器同理，
    环境变量 RBOT_ENABLE_INSPECT_SCHEDULER=1），测试不受影响。
    """
    if config.ENABLE_PLAN_SCHEDULER:
        from web.tasks import start_plan_scheduler

        start_plan_scheduler()
    if inspect_routes.ENABLE_INSPECT_SCHEDULER:
        inspect_routes.start_inspect_scheduler()
    yield
    from web.tasks import stop_plan_scheduler

    stop_plan_scheduler()
    inspect_routes.stop_inspect_scheduler()


def create_app():
    """FastAPI 应用工厂。

    ADMIN_PASSWORD_HASH 未设置时抛 RuntimeError 拒绝启动
    （测试时可通过环境变量 RBOT_WEB_PASSWORD_HASH 设一个测试哈希绕过）。
    """
    if not config.ADMIN_PASSWORD_HASH:
        raise RuntimeError(
            "未设置管理员密码哈希，拒绝启动。"
            "先运行 python3 -m web.auth 生成哈希，"
            "再设环境变量 RBOT_WEB_PASSWORD_HASH 后启动。")

    app = FastAPI(title="M_Bot Web 面板", version="1.2.0",
                  lifespan=_lifespan)

    # 统一错误信封：HTTPException / 参数校验失败也返回
    # {"ok": false, "error": "..."}，与各路由的手动错误响应一致。
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code,
                            content={"ok": False, "error": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request,
                                           exc: RequestValidationError):
        return JSONResponse(status_code=422,
                            content={"ok": False,
                                     "error": f"请求参数错误：{exc}"})

    # 静态文件（前端子代理负责；目录不存在时跳过挂载，不报错）。
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    else:
        print(f"警告：静态文件目录不存在，跳过 /static 挂载：{static_dir}")

    # API 与页面路由。
    app.include_router(auth_routes.router)
    app.include_router(system_routes.router)
    app.include_router(instances_routes.router)
    app.include_router(volumes_routes.router)
    app.include_router(boot_backups_routes.router)
    app.include_router(gfw_routes.router)
    app.include_router(quota_routes.router)
    app.include_router(dns_routes.router)
    app.include_router(create_routes.router)
    app.include_router(hosts_routes.router)
    app.include_router(objectstorage_routes.router)
    app.include_router(email_routes.router)
    app.include_router(plans_routes.router)
    app.include_router(profiles_routes.router)
    # 阶段 5 工作包 E 新增路由：批量命令 / 终端收藏夹 / 证书 /
    # 巡检计划 / 云报告 / 监控规则 / 设置（OCI 代理）。
    app.include_router(batch_routes.router)
    app.include_router(favorites_routes.router)
    app.include_router(cert_routes.router)
    app.include_router(inspect_routes.router)
    app.include_router(cloudreport_routes.router)
    app.include_router(monitor_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(pages_routes.router)

    # WebSocket 路由（Web SSH 终端，另一个子代理在写）：
    # 导入失败时跳过并打印警告，不要让面板崩掉。
    try:
        from web.ssh import router as ssh_router
        app.include_router(ssh_router)
    except ImportError as exc:
        print(f"警告：Web SSH 模块未就绪，跳过 WebSocket 路由（{exc}）")

    # SFTP 文件面板路由（阶段 3b）：与 ssh router 同样的容错方式。
    try:
        from web.sftp import router as sftp_router
        app.include_router(sftp_router)
    except ImportError as exc:
        print(f"警告：SFTP 模块未就绪，跳过 SFTP 路由（{exc}）")

    return app


# 模块级 app 实例，供 `uvicorn web.app:app` 使用。
# 密码哈希未设置时不创建（create_app 会抛 RuntimeError；此时按
# 模块 docstring 先设 RBOT_WEB_PASSWORD_HASH 再启动）。测试 import
# 本模块时哈希未设置，不会触发创建，不影响测试。
if config.ADMIN_PASSWORD_HASH:
    app = create_app()


if __name__ == "__main__":
    import uvicorn

    try:
        _app = create_app()
    except RuntimeError as exc:
        print(f"启动失败：{exc}")
        sys.exit(1)
    uvicorn.run(_app, host=config.HOST, port=config.PORT)
