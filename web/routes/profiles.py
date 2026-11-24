#!/usr/bin/env python3
"""账号（profile）列表路由。

  - GET /api/profiles：返回已配置的账号列表，供前端 profile 下拉动态填充。
    每个 profile 就是一个账号；多区订阅的账号带 regions 列表（订阅的全部
    区域，主区排第一），前端据此显示二级区域下拉（单区账号不显示）。
    列表还带 key_file（配置里的私钥路径）与 key_ready（私钥是否已在
    服务器上），供云账户页显示私钥状态与补传私钥。
  - POST /api/profiles/{profile}/key：给已配置的账号补传/替换私钥
    （multipart：key_file 字段）。内容必须是私钥，路径必须在 home 下，
    写盘权限 600；响应不回显私钥内容。

返回形状：{"ok": true, "default": "DEFAULT",
          "profiles": [{name, region, regionLabel, regionCount, regions,
                        displayName}, ...]}。
regionLabel 是区域中文名（新加坡/圣何塞/芝加哥），未列出的区域返回原代码。
regionCount 是该账号订阅的区域数（查 iam region-subscription list，
缓存 1 小时；查失败时为 null）；regions 是订阅区域列表（{code, label,
isHome}，主区排第一，其余按代码排序；订阅列表查失败时只含主区）；
displayName 是下拉显示名（主区中文名，多区账号后缀“（N 个区）”，与
机器人切换菜单一致）。

  - POST /api/profiles/parse：解析待添加的 OCI 配置（粘贴文本或上传
    config 文件），自动过滤已存在的 profile，返回待确认列表与
    pending_id（10 分钟有效，一次性）。解析复用机器人 /uploadapi 的
    parse_oci_config_text / prepare_oci_profiles，行为与机器人一致。
  - POST /api/profiles/commit：确认添加（multipart：pending_id +
    pem_<PROFILE> 私钥文件）。先校验全部 pem 内容（必须是私钥），再写
    配置（复用 commit_oci_profiles）、写 pem 文件（600）、保存
    [cloudflare] 段（复用 commit_cf_section）、补 compartment 环境变量
    （复用 add_profile_compartments）。新账号要重启机器人与面板后生效。
    响应里不回显 OCID、指纹与 Cloudflare 凭据值。
"""

import os
import secrets
import time

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from oci.client import read_oci_config_profiles
from tgbot import handlers_client
from tgbot.cf_config import mask_cf_email
from tgbot.handlers_client import (
    add_profile_compartments,
    commit_cf_section,
    commit_oci_profiles,
    parse_oci_config_text,
    prepare_oci_profiles,
)
from web import config
from web.deps import (
    error_response,
    get_current_user,
    get_profile_regions,
    get_region_counts,
)

router = APIRouter()

# 待确认导入的有效期（秒）与上传大小上限。
PENDING_EXPIRE_SECONDS = 600
MAX_CONFIG_BYTES = 65536
MAX_PEM_BYTES = 16384


class PendingProfileStore:
    """待确认的租户导入：pending_id -> 待导入内容（内存，10 分钟过期）。

    与 web/auth.py 的 MagicLoginStore 同构：token 32 字节随机（token 值
    绝不写日志），consume() 无论成功失败都删除该条（一次性，防重放）。
    面板是单 worker uvicorn，内存存储够用。
    """

    def __init__(self, expire_seconds=PENDING_EXPIRE_SECONDS):
        self.expire_seconds = expire_seconds
        self._pending = {}

    def create(self, profiles, cloudflare):
        """生成一个 pending_id，返回 pending_id 字符串。"""
        pending_id = secrets.token_urlsafe(24)
        self._pending[pending_id] = {
            "profiles": profiles,
            "cloudflare": cloudflare,
            "expires": time.time() + self.expire_seconds,
        }
        return pending_id

    def consume(self, pending_id):
        """消费一个 pending_id：存在且未过期返回内容，否则 None。

        无论结果都删除该条（一次性）；空 pending_id 直接返回 None。
        """
        if not pending_id:
            return None
        item = self._pending.pop(pending_id, None)
        if item is None or item["expires"] <= time.time():
            return None
        return item

    def reset(self):
        """清空全部待确认导入（测试用）。"""
        self._pending.clear()


PENDING_PROFILES = PendingProfileStore()


def _is_private_key_pem(data):
    """上传内容是不是私钥文件：UTF-8 文本且含私钥 BEGIN/END 行。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return ("-----BEGIN" in text and "PRIVATE KEY-----" in text
            and "-----END" in text)


def _key_path_under_home(path):
    """key_file 解析后是否在 root 家目录下（防路径穿越写到别处）。"""
    home = os.path.abspath(os.path.expanduser("~"))
    target = os.path.abspath(os.path.expanduser(path or ""))
    return target == home or target.startswith(home + os.sep)


def _configured_key_files():
    """已配置账号的 key_file：{profile 名: key_file 路径}。

    读的是与机器人 /uploadapi 同一份配置文件（handlers_client 的
    OCI_CONFIG_PATH），只读配置文本，不读私钥文件本身。
    """
    try:
        parsed = read_oci_config_profiles(
            handlers_client.OCI_CONFIG_PATH)
    except Exception:  # noqa: BLE001 - 配置读不到时当作无私钥信息
        return {}
    return {name: (fields.get("key_file") or "")
            for name, fields in parsed.items()}


def _ordered_regions(name, home_region, codes):
    """订阅区域列表（主区排第一，其余按代码排序）；查不到订阅列表时
    只返回主区（前端按单区处理，不显示二级菜单）。"""
    seen = {home_region}
    ordered = [home_region]
    for code in sorted(codes or []):
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return [{"code": code,
             "label": config.get_region_label(code),
             "isHome": code == home_region} for code in ordered]


@router.get("/api/profiles")
def list_profiles(username: str = Depends(get_current_user)):
    """列出已配置的账号（profile）：名字、区域、区域中文名、订阅区数、
    订阅区域列表、显示名、私钥路径与私钥是否就位。"""
    counts = get_region_counts()
    regions = get_profile_regions()
    key_files = _configured_key_files()
    profiles = [
        {
            "name": name,
            "region": cfg["region"],
            "regionLabel": config.get_region_label(cfg["region"]),
            "regionCount": counts.get(name),
            "regions": _ordered_regions(name, cfg["region"],
                                        regions.get(name)),
            "displayName": config.profile_display_name(
                name, counts.get(name)),
            "key_file": key_files.get(name, ""),
            "key_ready": bool(key_files.get(name))
                         and os.path.exists(key_files[name]),
        }
        for name, cfg in config.PROFILES.items()
    ]
    return {"ok": True, "default": config.DEFAULT_PROFILE,
            "profiles": profiles}


@router.post("/api/profiles/{profile}/key")
async def upload_profile_key(profile: str,
                             key_file: UploadFile = File(...),
                             username: str = Depends(get_current_user)):
    """给已配置的账号补传/替换私钥（multipart，key_file 字段）。

    写到该账号配置里的 key_file 路径（必须是 home 下的路径，写盘
    权限 600）。内容必须是私钥文件，否则什么都不写。响应不回显
    私钥内容。多账号共用同一 key_file 时，传一次就全好了。
    """
    if profile not in config.PROFILES:
        return error_response(f"没有这个账号：{profile}", status_code=404)
    target = _configured_key_files().get(profile, "")
    if not target:
        return error_response(f"账号「{profile}」的配置里没有 key_file",
                              status_code=400)
    if not _key_path_under_home(target):
        return error_response("私钥路径不在 home 下，拒绝写入",
                              status_code=400)
    raw = await key_file.read(MAX_PEM_BYTES + 1)
    await key_file.close()
    if len(raw) > MAX_PEM_BYTES:
        return error_response("私钥文件超过 16KB", status_code=400)
    if not _is_private_key_pem(raw):
        return error_response("上传的不是私钥文件"
                              "（需含 -----BEGIN ... PRIVATE KEY-----）",
                              status_code=400)
    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(raw)
        os.chmod(target, 0o600)
    except OSError as exc:
        return error_response(f"写入私钥失败：{exc}", status_code=500)
    return {"ok": True, "profile": profile, "key_file": target,
            "key_ready": True}


@router.post("/api/profiles/parse")
async def parse_profiles(config_text: str = Form(default=""),
                         config_file: UploadFile = File(default=None),
                         username: str = Depends(get_current_user)):
    """解析待添加的 OCI 配置：粘贴文本或上传 config 文件（二选一，
    两个都给时以文件为准）。

    自动过滤已存在的 profile（与机器人 /uploadapi 一致）；返回待确认
    列表与 pending_id。响应里不回显 OCID、指纹与凭据值。
    """
    text = (config_text or "").strip()
    if config_file is not None and config_file.filename:
        raw = await config_file.read(MAX_CONFIG_BYTES + 1)
        await config_file.close()
        if len(raw) > MAX_CONFIG_BYTES:
            return error_response("配置文件超过 64KB", status_code=400)
        try:
            file_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return error_response("配置文件不是 UTF-8 文本",
                                  status_code=400)
        if file_text.strip():
            text = file_text
    if not text.strip():
        return error_response("请粘贴 config 文本或上传 config 文件",
                              status_code=400)
    parsed = parse_oci_config_text(text)
    if parsed.get("error"):
        return error_response(parsed["error"], status_code=400)
    new_profiles, skipped, prep_error = prepare_oci_profiles(parsed)
    if prep_error:
        return error_response(prep_error, status_code=400)
    cloudflare = parsed.get("cloudflare")
    if not new_profiles and not cloudflare:
        return error_response("没有可添加的新账号（全部已存在）",
                              status_code=400)
    pending_id = PENDING_PROFILES.create(new_profiles, cloudflare)
    items = []
    for entry in new_profiles:
        fields = entry["fields"]
        key_file = fields.get("key_file", "")
        items.append({
            "profile": entry["profile"],
            "region": fields.get("region", ""),
            "regionLabel": config.get_region_label(
                fields.get("region", "")),
            "key_file": key_file,
            "key_ready": bool(key_file) and os.path.exists(key_file),
        })
    cf_info = None
    if cloudflare:
        email = cloudflare.get("email") or ""
        cf_info = {
            "present": True,
            "has_token": bool(cloudflare.get("api_token")),
            "email_masked": mask_cf_email(email) if email else None,
        }
    return {"ok": True, "pending_id": pending_id, "profiles": items,
            "skipped": skipped, "cloudflare": cf_info}


@router.post("/api/profiles/commit")
async def commit_profiles(request: Request,
                          username: str = Depends(get_current_user)):
    """确认添加租户（multipart 表单）。

    字段：pending_id（解析步骤返回的，一次性）与 pem_<PROFILE>（每个
    需私钥的 profile 各一个文件字段，可留空——留空且服务器上还没有
    私钥时，该账号仍会写入配置，但要自行 scp 私钥后才能用）。

    顺序：先校验全部 pem 内容（失败则什么都不写）→ 写配置 → 写 pem
    文件（600）→ 保存 [cloudflare] 段 → 补 compartment 环境变量。
    """
    form = await request.form()
    pending = PENDING_PROFILES.consume(form.get("pending_id"))
    if pending is None:
        return error_response("确认已过期或无效，请重新解析",
                              status_code=400)
    new_profiles = pending["profiles"]
    # 1. 先校验全部上传的 pem（写盘前校验，任一失败则什么都不写）。
    pems = {}
    for entry in new_profiles:
        profile = entry["profile"]
        upload = form.get(f"pem_{profile}")
        if upload is None or not hasattr(upload, "read"):
            continue
        raw = await upload.read(MAX_PEM_BYTES + 1)
        await upload.close()
        if len(raw) > MAX_PEM_BYTES:
            return error_response(
                f"profile「{profile}」的私钥文件超过 16KB",
                status_code=400)
        if not _is_private_key_pem(raw):
            return error_response(
                f"profile「{profile}」上传的不是私钥文件"
                "（需含 -----BEGIN ... PRIVATE KEY-----）",
                status_code=400)
        pems[profile] = raw
    # 2. 写配置（复用机器人逻辑：写段 + 复制已在服务器上的 key）。
    try:
        results = commit_oci_profiles(new_profiles)
    except (ValueError, OSError) as exc:
        return error_response(f"写入配置失败：{exc}", status_code=500)
    committed = {item["profile"] for item in results}
    # 3. 写上传的 pem（只写已提交成功的 profile；路径必在 home 下，
    # 上传的私钥覆盖自动复制来的旧 key）。
    pem_failed = []
    for profile, raw in pems.items():
        if profile not in committed:
            continue
        key_file = next(item["key_file"] for item in results
                        if item["profile"] == profile)
        if not key_file or not _key_path_under_home(key_file):
            pem_failed.append(profile)
            continue
        try:
            parent = os.path.dirname(key_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(key_file, "wb") as handle:
                handle.write(raw)
            os.chmod(key_file, 0o600)
        except OSError:
            pem_failed.append(profile)
    # 4. [cloudflare] 段 + compartment 环境变量（失败不影响已写入的账号）。
    cf_status = "skipped"
    if pending.get("cloudflare"):
        try:
            cf_status = commit_cf_section(pending["cloudflare"])
        except Exception:  # noqa: BLE001 - CF 保存失败只记状态
            cf_status = "failed"
    compartments = {}
    try:
        compartments = add_profile_compartments(new_profiles)
    except Exception:  # noqa: BLE001 - env 写入失败只记空结果
        compartments = {}
    keys = []
    for item in results:
        keys.append({
            "profile": item["profile"],
            "key_file": item["key_file"],
            "key_ready": bool(item["key_ready"])
                         or item["profile"] in pems
                         or (item["key_file"]
                             and os.path.exists(item["key_file"])),
        })
    return {"ok": True,
            "added": [item["profile"] for item in results],
            "keys": keys,
            "pem_failed": pem_failed,
            "cloudflare": cf_status,
            "compartments": sorted(compartments),
            "restart_needed": bool(results)}
