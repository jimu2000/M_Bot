#!/usr/bin/env python3
"""阶段 5 工作包 E：部署脚本与文档的测试。

覆盖：scripts/deploy.sh 语法检查（bash -n，不执行）、借鉴点标记齐全、
docs/deployment.md 与 docs/phase5-todo.md 存在且含关键内容。
不执行部署脚本，不碰真实服务器。
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_SH = os.path.join(PROJECT_ROOT, "scripts", "deploy.sh")
DEPLOYMENT_MD = os.path.join(PROJECT_ROOT, "docs", "deployment.md")
TODO_MD = os.path.join(PROJECT_ROOT, "docs", "phase5-todo.md")


class DeployScriptTest(unittest.TestCase):
    """scripts/deploy.sh：语法 + 借鉴点标记。"""

    def test_bash_syntax_ok(self):
        result = subprocess.run(["bash", "-n", DEPLOY_SH],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_is_executable(self):
        self.assertTrue(os.access(DEPLOY_SH, os.X_OK))

    def test_borrowed_points_present(self):
        with open(DEPLOY_SH, encoding="utf-8") as f:
            content = f.read()
        # systemd 自动拉起。
        self.assertIn("Restart=always", content)
        self.assertIn("RestartSec=10", content)
        # 原子升级：同文件系统临时目录 + mv 覆盖。
        self.assertIn("mktemp -d", content)
        self.assertIn("mv ", content)
        # 升级锁。
        self.assertIn("flock", content)
        # 端口解析优先级：参数 > 进程命令行 > 文件 > 默认。
        self.assertIn("resolve_port", content)
        self.assertIn("--port=", content)
        self.assertIn("PORT_FILE", content)
        self.assertIn("DEFAULT_PORT=9527", content)
        # 备份与回滚。
        self.assertIn("rollback", content)
        self.assertIn("BACKUP_DIR", content)
        # 健康检查。
        self.assertIn("health_check", content)

    def test_no_real_secrets(self):
        """模板里不含真实 IP / 密码 / Token（只有占位符）。"""
        with open(DEPLOY_SH, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("RBOT_WEB_PASSWORD_HASH='", content)
        self.assertIn("<你的账号>", content)
        self.assertIn("<面板服务器 IP>", content)


class DeploymentDocsTest(unittest.TestCase):
    """docs/deployment.md 与 docs/phase5-todo.md。"""

    def test_deployment_md(self):
        self.assertTrue(os.path.exists(DEPLOYMENT_MD))
        with open(DEPLOYMENT_MD, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("部署步骤", content)
        self.assertIn("回滚", content)
        self.assertIn("Restart=always", content)
        self.assertIn("flock", content)
        self.assertIn("端口解析", content)
        self.assertIn("原子升级", content)

    def test_phase5_todo(self):
        """两项低优先级缺口标注待做并写清原因。"""
        self.assertTrue(os.path.exists(TODO_MD))
        with open(TODO_MD, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("resolve-by-ip", content)
        self.assertIn("paste-image", content)
        self.assertIn("剪贴板", content)
        self.assertIn("工作量与收益不成正比", content)


if __name__ == "__main__":
    unittest.main()
