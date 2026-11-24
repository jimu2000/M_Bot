#!/usr/bin/env python3
"""HTML 页面路由（Jinja2 模板，目录 web/templates）。

  - GET / → 重定向 /overview。
  - GET /login、/overview、/instances、/create、/quota、/volumes、/tasks、
    /dns、/terminal、/objectstorage、/email、/cert、/inspect。
  - 除 /login 外，未登录时全部重定向到 /login。

模板文件由前端子代理负责；模板缺失时 _render() 返回最小占位 HTML，
不报错（方便后端先行联调）。
"""

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from web import config
from web.auth import SESSIONS

router = APIRouter()

_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# 模板缺失时的最小占位页面（前端模板到位后不再用到）。
_PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
<h1>{title}</h1>
<p>页面模板 {name} 尚未就绪（前端开发中），请稍后再来。</p>
<p><a href="/login">去登录</a> · <a href="/overview">去概览</a></p>
</body>
</html>"""


def _is_logged_in(request):
    """请求是否带有效会话 cookie。"""
    token = request.cookies.get(config.SESSION_COOKIE)
    return bool(token and SESSIONS.get(token))


def _current_username(request):
    """当前登录用户名；未登录返回 None。"""
    token = request.cookies.get(config.SESSION_COOKIE)
    return SESSIONS.get(token) if token else None


def _render(request, name, title, context=None):
    """渲染模板；模板不存在或渲染失败时返回最小占位 HTML。

    每个页面自动带上 current_page（模板文件名去后缀，供侧边栏高亮）
    与 username（当前登录用户，供顶栏显示）。
    """
    ctx = {
        "request": request,
        "title": title,
        "current_page": os.path.splitext(os.path.basename(name))[0],
        "username": _current_username(request),
    }
    ctx.update(context or {})
    try:
        return templates.TemplateResponse(request, name, ctx)
    except Exception:
        return HTMLResponse(
            _PLACEHOLDER_HTML.format(title=title, name=name))


def _require_login(request):
    """未登录时返回到 /login 的重定向；已登录返回 None。"""
    if not _is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return None


@router.get("/")
def index(request: Request):
    """根路径重定向到概览。"""
    return RedirectResponse("/overview", status_code=302)


@router.get("/login")
def login_page(request: Request):
    """登录页（不需要登录）。Clicky 只在配置了站点 ID 时加载。"""
    return _render(request, "login.html", "登录",
                   {"clarity_id": config.CLARITY_ID})


@router.get("/overview")
def overview_page(request: Request):
    """概览页。"""
    redirect = _require_login(request)
    return redirect or _render(request, "overview.html", "概览")


@router.get("/instances")
def instances_page(request: Request):
    """实例管理页。"""
    redirect = _require_login(request)
    return redirect or _render(request, "instances.html", "实例管理")


@router.get("/create")
def create_page(request: Request):
    """创建实例页。"""
    redirect = _require_login(request)
    return redirect or _render(request, "create.html", "创建实例")


@router.get("/quota")
def quota_page(request: Request):
    """配额页（免费额度汇总）。"""
    redirect = _require_login(request)
    return redirect or _render(request, "quota.html", "配额")


@router.get("/volumes")
def volumes_page(request: Request):
    """启动盘页。"""
    redirect = _require_login(request)
    return redirect or _render(request, "volumes.html", "启动盘")


@router.get("/gfw")
def gfw_page(request: Request):
    """GFW 自动换 IP 页（菜单 16 的网页版）。"""
    redirect = _require_login(request)
    return redirect or _render(request, "gfw.html", "GFW 自动换 IP")


@router.get("/bark")
def bark_page(request: Request):
    """Bark 通知设置页（菜单 43 的网页版）。"""
    redirect = _require_login(request)
    return redirect or _render(request, "bark.html", "Bark 通知设置")


@router.get("/tasks")
def tasks_page(request: Request):
    """任务管理页。"""
    redirect = _require_login(request)
    return redirect or _render(request, "tasks.html", "任务管理")


@router.get("/dns")
def dns_page(request: Request):
    """DNS 管理页（Cloudflare DNS 记录管理）。"""
    redirect = _require_login(request)
    return redirect or _render(request, "dns.html", "DNS 管理")


@router.get("/objectstorage")
def objectstorage_page(request: Request):
    """对象存储页（Bucket 与对象管理）。"""
    redirect = _require_login(request)
    return redirect or _render(request, "objectstorage.html", "对象存储")


@router.get("/email")
def email_page(request: Request):
    """Email Delivery 页（发件人 + SMTP 凭据一键搭建）。"""
    redirect = _require_login(request)
    return redirect or _render(request, "email.html", "Email Delivery")


@router.get("/terminal")
def terminal_page(request: Request):
    """SSH 终端页。"""
    redirect = _require_login(request)
    return redirect or _render(request, "terminal.html", "SSH 终端")


@router.get("/cert")
def cert_page(request: Request):
    """证书管理页（ACME 证书状态与续期，阶段 5 工作包 E）。"""
    redirect = _require_login(request)
    return redirect or _render(request, "cert.html", "证书管理")


@router.get("/accounts")
def accounts_page(request: Request):
    """云账户页（已配置账号列表 + 添加租户）。"""
    redirect = _require_login(request)
    return redirect or _render(request, "accounts.html", "云账户")


@router.get("/inspect")
def inspect_page(request: Request):
    """巡检计划页（阶段 5 工作包 E）。"""
    redirect = _require_login(request)
    return redirect or _render(request, "inspect.html", "巡检计划")
