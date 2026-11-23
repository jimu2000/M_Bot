#!/usr/bin/env python3
"""client.py 的单元测试（mock，不碰真实 API）。

运行：python3 -m pytest tests/test_client.py -v
或：   python3 -m unittest tests.test_client -v
"""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import (AuthError, NotFoundError, OCIClient, OCIError,
                        OutOfCapacityError, QuotaExceededError,
                        RateLimitError, classify_error)

# 占位符（测试里不出现真实 OCID）
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
        # OCI 对不存在的资源返回 NotAuthorizedOrNotFound（404），必须判为 NotFound
        # 而不是 AuthError（实测：2026-11-23）。
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


if __name__ == "__main__":
    unittest.main()
