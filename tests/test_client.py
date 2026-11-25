#!/usr/bin/env python3
"""client.py 的单元测试（mock，不碰真实 API）。

运行：python3 -m pytest tests/test_client.py -v
或：   python3 -m unittest tests.test_client -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import (AuthError, NotFoundError, OCIClient, OCIError,
                        OutOfCapacityError, QuotaExceededError,
                        RateLimitError, classify_error,
                        read_oci_config_profiles, resolve_oci_config_path)
import oci.client as oci_client_mod

# Placeholders (no real OCID in tests). 占位符（测试里不出现真实 OCID）。
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["oci"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class TestClassifyError(unittest.TestCase):
    def test_out_of_capacity(self):
        self.assertIs(classify_error("Out of host capacity"), OutOfCapacityError)

    def test_rate_limit(self):
        self.assertIs(classify_error("TooManyRequests"), RateLimitError)
        self.assertIs(classify_error("HTTP 429"), RateLimitError)

    def test_quota(self):
        self.assertIs(classify_error("LimitExceeded"), QuotaExceededError)
        self.assertIs(classify_error("QuotaExceeded"), QuotaExceededError)
        self.assertIs(classify_error("service limits were exceeded"),
                      QuotaExceededError)

    def test_auth(self):
        self.assertIs(classify_error("NotAuthenticated"), AuthError)
        self.assertIs(classify_error("NotAuthorized"), AuthError)

    def test_not_found(self):
        self.assertIs(classify_error("NotFound"), NotFoundError)

    def test_not_authorized_or_not_found(self):
        # OCI returns NotAuthorizedOrNotFound (404) for a missing resource; it must map to NotFound, not AuthError. OCI 对不存在的资源返回 NotAuthorizedOrNotFound（404），必须判为 NotFound 而不是 AuthError。
        self.assertIs(classify_error('"code": "NotAuthorizedOrNotFound"'),
                      NotFoundError)

    def test_unknown_falls_back_to_base(self):
        self.assertIs(classify_error("something totally different"), OCIError)


class TestOCIClient(unittest.TestCase):
    def setUp(self):
        self.cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")

    @patch("oci.client.subprocess.run")
    def test_run_parses_json(self, mock_run):
        mock_run.return_value = _completed(stdout=json.dumps({"data": [1, 2]}))
        result = self.cli.run(["compute", "instance", "list",
                               "--compartment-id", FAKE_COMPARTMENT])
        self.assertEqual(result, {"data": [1, 2]})

    @patch("oci.client.subprocess.run")
    def test_run_appends_profile_and_region(self, mock_run):
        mock_run.return_value = _completed(stdout="{}")
        self.cli.run(["compute", "instance", "list"])
        cmd = mock_run.call_args[0][0]
        self.assertIn("--profile", cmd)
        self.assertIn("DEFAULT", cmd)
        self.assertIn("--region", cmd)
        self.assertIn("ap-singapore-1", cmd)

    @patch("oci.client.subprocess.run")
    def test_run_sets_suppress_label_warning(self, mock_run):
        mock_run.return_value = _completed(stdout="{}")
        self.cli.run(["compute", "instance", "list"])
        env = mock_run.call_args[1]["env"]
        self.assertEqual(env.get("SUPPRESS_LABEL_WARNING"), "True")

    @patch("oci.client.subprocess.run")
    def test_run_sets_config_file_env(self, mock_run):
        """每次调用都把解析出的配置文件路径传给子进程。"""
        mock_run.return_value = _completed(stdout="{}")
        with patch.object(oci_client_mod, "resolve_oci_config_path",
                          return_value="/root/oci_config"):
            self.cli.run(["compute", "instance", "list"])
        env = mock_run.call_args[1]["env"]
        self.assertEqual(env.get("OCI_CLI_CONFIG_FILE"), "/root/oci_config")

    @patch("oci.client.subprocess.run")
    def test_run_raises_classified_error(self, mock_run):
        mock_run.return_value = _completed(
            returncode=1, stderr="ServiceError: Out of host capacity")
        with self.assertRaises(OutOfCapacityError):
            self.cli.run(["compute", "instance", "launch"])

    @patch("oci.client.subprocess.run")
    def test_run_raises_rate_limit(self, mock_run):
        mock_run.return_value = _completed(
            returncode=1, stderr="ServiceError: TooManyRequests")
        with self.assertRaises(RateLimitError):
            self.cli.run(["compute", "instance", "list"])

    @patch("oci.client.subprocess.run")
    def test_run_raises_quota(self, mock_run):
        mock_run.return_value = _completed(
            returncode=1, stderr="ServiceError: LimitExceeded")
        with self.assertRaises(QuotaExceededError):
            self.cli.run(["compute", "instance", "launch"])

    @patch("oci.client.subprocess.run")
    def test_run_raises_auth(self, mock_run):
        mock_run.return_value = _completed(
            returncode=1, stderr="ServiceError: NotAuthenticated")
        with self.assertRaises(AuthError):
            self.cli.run(["compute", "instance", "list"])

    @patch("oci.client.subprocess.run")
    def test_run_text_returns_raw_stdout(self, mock_run):
        mock_run.return_value = _completed(stdout="ocid1.instance.oc1..yyyy\n")
        result = self.cli.run_text(["compute", "instance", "list",
                                    "--query", "data[0].id", "--raw-output"])
        self.assertEqual(result, "ocid1.instance.oc1..yyyy")

    @patch("oci.client.subprocess.run")
    def test_run_empty_output_returns_none(self, mock_run):
        mock_run.return_value = _completed(stdout="")
        self.assertIsNone(self.cli.run(["compute", "instance", "list"]))

    @patch("oci.client.subprocess.run")
    def test_per_call_profile_region_override(self, mock_run):
        mock_run.return_value = _completed(stdout="{}")
        self.cli.run(["compute", "instance", "list"],
                     profile="ACCOUNT2", region="us-sanjose-1")
        cmd = mock_run.call_args[0][0]
        self.assertIn("ACCOUNT2", cmd)
        self.assertIn("us-sanjose-1", cmd)


class TestResolveOciConfigPath(unittest.TestCase):
    """v1.28.0：配置文件路径解析（~/oci_config 优先，旧路径回退）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "oci_config")
        self.legacy = os.path.join(self._tmp.name, "legacy", "config")
        for attr, path in (("OCI_CONFIG_ROOT_PATH", self.root),
                           ("OCI_CONFIG_LEGACY_PATH", self.legacy)):
            p = patch.object(oci_client_mod, attr, path)
            p.start()
            self.addCleanup(p.stop)
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("MBOT_OCI_CONFIG_FILE", None)
        os.environ.pop("OCI_CLI_CONFIG_FILE", None)

    def test_mbot_env_var_wins(self):
        with patch.dict(os.environ,
                        {"MBOT_OCI_CONFIG_FILE": "/tmp/custom-config"}):
            self.assertEqual(resolve_oci_config_path(), "/tmp/custom-config")

    def test_official_env_var_second(self):
        with patch.dict(os.environ,
                        {"OCI_CLI_CONFIG_FILE": "/tmp/official-config"}):
            self.assertEqual(resolve_oci_config_path(),
                             "/tmp/official-config")

    def test_root_file_preferred_when_exists(self):
        with open(self.root, "w") as f:
            f.write("[DEFAULT]\n")
        os.makedirs(os.path.dirname(self.legacy), exist_ok=True)
        with open(self.legacy, "w") as f:
            f.write("[DEFAULT]\n")
        self.assertEqual(resolve_oci_config_path(), self.root)

    def test_legacy_fallback_when_root_missing(self):
        os.makedirs(os.path.dirname(self.legacy), exist_ok=True)
        with open(self.legacy, "w") as f:
            f.write("[DEFAULT]\n")
        self.assertEqual(resolve_oci_config_path(), self.legacy)

    def test_neither_exists_returns_root_path(self):
        self.assertEqual(resolve_oci_config_path(), self.root)


class TestReadOciConfigProfiles(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "oci_config")

    def test_reads_all_profiles_including_default(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[DEFAULT]\nregion = ap-singapore-1\n"
                    "tenancy = ocid1.tenancy.oc1..fake\n"
                    "[SG1]\nregion = ap-singapore-1\n")
        profiles = read_oci_config_profiles(self.path)
        self.assertEqual(profiles["DEFAULT"]["region"], "ap-singapore-1")
        self.assertEqual(profiles["SG1"]["region"], "ap-singapore-1")

    def test_missing_file_returns_empty(self):
        self.assertEqual(read_oci_config_profiles(self.path), {})

    def test_broken_file_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not a config file at all\n")
        self.assertEqual(read_oci_config_profiles(self.path), {})


if __name__ == "__main__":
    unittest.main()
