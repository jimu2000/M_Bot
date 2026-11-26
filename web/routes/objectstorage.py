#!/usr/bin/env python3
"""对象存储路由（Bucket 与对象管理）。

  - GET /api/objectstorage/buckets：Bucket 列表。
  - POST /api/objectstorage/buckets：新建 Bucket（body {name}）。
  - DELETE /api/objectstorage/buckets/{name}：删除 Bucket
    （?empty=true 时连对象一起删，不可恢复）。
  - GET /api/objectstorage/buckets/{name}/objects：对象列表。
  - POST /api/objectstorage/buckets/{name}/objects：上传对象
    （multipart 表单：file 文件 + 可选 name 对象名）。
  - DELETE /api/objectstorage/buckets/{name}/objects/{object}：
    删除对象（不可恢复）。

命名空间每次调用时现查（oci os ns get），不缓存。
全部接口需登录（Depends(get_current_user)）。
"""

import os
import tempfile
import threading
import time

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel

from oci.client import OCIError
from oci.objectstorage import (
    create_bucket,
    delete_bucket,
    delete_object,
    get_namespace,
    list_buckets,
    list_objects,
    upload_object,
)
from web import config
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)

router = APIRouter()

# ================= 配置区（改这里） =================
# Bucket list server cache TTL (seconds): each list call runs 2 OCI CLI calls (namespace lookup + list), several seconds serially; repeat visits within 30s return the cache, and a manual refresh (refresh=1) bypasses it. Bucket 列表服务端缓存 TTL（秒）：列表接口每次要跑 2 次 OCI CLI（查命名空间 + list），串行几秒；30 秒内重复访问直接返回缓存，手动刷新（refresh=1）绕过缓存。
OBJECTSTORAGE_CACHE_TTL = 30
# =====================================================

# Bucket list cache: (profile, region) -> {"expires": monotonic-clock expiry, "payload": response body}. Memory only, never written to disk. Bucket 列表缓存：(profile, region) -> {"expires": 单调时钟过期点, "payload": 响应体}。只放内存不写盘。
_BUCKETS_CACHE = {}
_BUCKETS_CACHE_LOCK = threading.Lock()


def clear_objectstorage_cache(profile=None):
    """清对象存储 Bucket 列表缓存：指定 profile 时只清该账号，否则全清。

    写操作（新建/删除 Bucket）成功后调用，避免列表显示旧数据；
    单元测试在 setUp 里调用，避免缓存跨测试串数据。
    """
    with _BUCKETS_CACHE_LOCK:
        if profile is None:
            _BUCKETS_CACHE.clear()
        else:
            for key in [k for k in _BUCKETS_CACHE if k[0] == profile]:
                del _BUCKETS_CACHE[key]


class CreateBucketBody(BaseModel):
    """新建 Bucket 请求体。"""

    name: str


def _resolve_namespace(cli):
    """查当前租户的对象存储命名空间（每次调用现查）。"""
    return get_namespace(cli)


@router.get("/api/objectstorage/buckets")
def list_all_buckets(profile: str = Query(default=None),
                     region: str = Query(default=None),
                     refresh: bool = Query(default=False),
                     username: str = Depends(get_current_user)):
    """Bucket 列表（30 秒服务端缓存；refresh=true 绕过缓存）。

    refresh=true：绕过服务端缓存，强制重新查询（前端刷新按钮用）。
    """
    try:
        cli, compartment_id = resolve_profile(profile, region)
        profile_name = profile or config.DEFAULT_PROFILE
        cache_key = (profile_name, cli.region)
        if not refresh:
            with _BUCKETS_CACHE_LOCK:
                entry = _BUCKETS_CACHE.get(cache_key)
            if entry and time.monotonic() < entry["expires"]:
                return entry["payload"]
        namespace = _resolve_namespace(cli)
        buckets = list_buckets(cli, compartment_id, namespace=namespace)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    payload = {"ok": True,
               "profile": profile_name,
               "buckets": buckets}
    with _BUCKETS_CACHE_LOCK:
        _BUCKETS_CACHE[cache_key] = {
            "expires": time.monotonic() + OBJECTSTORAGE_CACHE_TTL,
            "payload": payload,
        }
    return payload


@router.post("/api/objectstorage/buckets", status_code=201)
def create_new_bucket(body: CreateBucketBody,
                      profile: str = Query(default=None),
                      region: str = Query(default=None),
                      username: str = Depends(get_current_user)):
    """新建 Bucket（默认私有访问、标准存储层）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        namespace = _resolve_namespace(cli)
        bucket = create_bucket(cli, compartment_id, body.name,
                               namespace=namespace)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_objectstorage_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "bucket": bucket}


@router.delete("/api/objectstorage/buckets/{bucket_name}")
def delete_bucket_route(bucket_name: str,
                        empty: bool = Query(default=False),
                        profile: str = Query(default=None),
                        region: str = Query(default=None),
                        username: str = Depends(get_current_user)):
    """删除 Bucket（只能删空 Bucket；empty=true 时连对象一起删，不可恢复）。"""
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        namespace = _resolve_namespace(cli)
        delete_bucket(cli, bucket_name, namespace=namespace, empty=empty)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_objectstorage_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "bucket_name": bucket_name}


@router.get("/api/objectstorage/buckets/{bucket_name}/objects")
def list_bucket_objects(bucket_name: str,
                        profile: str = Query(default=None),
                        region: str = Query(default=None),
                        username: str = Depends(get_current_user)):
    """某 Bucket 下的对象列表。"""
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        namespace = _resolve_namespace(cli)
        objects = list_objects(cli, bucket_name, namespace=namespace)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "bucket_name": bucket_name, "objects": objects}


@router.post("/api/objectstorage/buckets/{bucket_name}/objects",
             status_code=201)
async def upload_bucket_object(bucket_name: str,
                               file: UploadFile = File(...),
                               name: str = Form(default=None),
                               profile: str = Query(default=None),
                               region: str = Query(default=None),
                               username: str = Depends(get_current_user)):
    """上传一个文件为对象（同名对象会被覆盖）。

    上传内容先写临时文件再调 OCI CLI（oci os object put 读本地文件），
    上传完删除临时文件。对象名默认用上传的文件名。
    """
    object_name = name or file.filename
    if not object_name:
        return error_response("对象名不能为空（请上传带文件名的文件，"
                              "或显式传 name 参数）", status_code=400)
    tmp_path = None
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        namespace = _resolve_namespace(cli)
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
            content = await file.read()
            tmp.write(content)
        result = upload_object(cli, bucket_name, object_name, tmp_path,
                               namespace=namespace,
                               content_type=file.content_type or None)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return {"ok": True, "bucket_name": bucket_name,
            "object_name": object_name, "object": result}


@router.delete("/api/objectstorage/buckets/{bucket_name}/objects/{object_name:path}")
def delete_bucket_object(bucket_name: str, object_name: str,
                         profile: str = Query(default=None),
                         region: str = Query(default=None),
                         username: str = Depends(get_current_user)):
    """删除 Bucket 里的一个对象（不可恢复）。

    object_name 用 :path 转换器：对象名里可能含斜杠（如 "dir/file.txt"）。
    """
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        namespace = _resolve_namespace(cli)
        delete_object(cli, bucket_name, object_name, namespace=namespace)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "bucket_name": bucket_name,
            "object_name": object_name}
