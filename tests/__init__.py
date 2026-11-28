"""测试包。"""

import os
import tempfile

# 全套件凭据库重定向：任何测试跑真实抢机线程 / 通知 / 创建接口时，
# 成功后都会把 root 密码写进实例凭据库；为防止测试把密码写进真实
# 数据目录，整个套件默认把库指到临时文件。个别测试如需断言库内容，
# 自行再 patch 到自己的临时文件即可覆盖（见 test_instance_credentials.py）。
# Suite-wide credential-store redirect: any test that runs the real
# grabber thread / notify path / create API would save the root password
# into the instance credential store on success; the whole suite
# therefore points the store at a temp file so no test can write
# passwords to the real data directory. Individual tests can patch it
# again to their own temp file when they need to assert on its contents.
_CRED_TMP = tempfile.TemporaryDirectory()
try:
    from tgbot import instance_credentials as _ic

    _ic.CREDENTIALS_FILE = os.path.join(
        _CRED_TMP.name, "instance_credentials.json")
except Exception:  # pragma: no cover - 导入失败时不阻塞测试收集
    pass
