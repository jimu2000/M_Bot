#!/usr/bin/env python3
"""clouds.cloudflare.check_ssl_expiry 的单元测试（阶段 5 工作包 C）。

全部用 mock，不做真实 TLS 连接。socket.create_connection 与
ssl.create_default_context 都 patch 掉，证书到期日用假数据。
测试域名一律用 example.com（测试专用域名）。
"""

import socket
import ssl
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from clouds.cloudflare import CloudflareError, check_ssl_expiry


def _cert_not_after(days_from_now):
    """生成距今天 N 天的证书 notAfter 字符串（证书格式）。"""
    when = datetime.now(timezone.utc) + timedelta(days=days_from_now)
    return when.strftime("%b %d %H:%M:%S %Y GMT")


def _mock_tls(cert):
    """mock TLS 握手：create_connection 与 wrap_socket 都返回假对象。

    返回 (create_connection mock, context mock)；cert 是
    getpeercert() 的返回值（dict 或 None）。
    """
    mock_sock = MagicMock()
    mock_sock.__enter__.return_value = mock_sock
    mock_create = MagicMock(return_value=mock_sock)

    mock_ssock = MagicMock()
    mock_ssock.__enter__.return_value = mock_ssock
    mock_ssock.getpeercert.return_value = cert
    mock_context = MagicMock()
    mock_context.wrap_socket.return_value = mock_ssock
    mock_create_context = MagicMock(return_value=mock_context)
    return mock_create, mock_create_context


class TestCheckSslExpiry(unittest.TestCase):

    def _run(self, cert, domain="example.com", port=443):
        mock_create, mock_create_context = _mock_tls(cert)
        with patch("clouds.cloudflare.socket.create_connection",
                   mock_create), \
             patch("clouds.cloudflare.ssl.create_default_context",
                   mock_create_context):
            result = check_ssl_expiry(domain, port=port)
        return result, mock_create, mock_create_context

    def test_returns_domain_expires_and_days_left(self):
        result, _, _ = self._run({"notAfter": _cert_not_after(45)})
        self.assertEqual(result["domain"], "example.com")
        expected = (datetime.now(timezone.utc)
                    + timedelta(days=45)).date().isoformat()
        self.assertEqual(result["expires"], expected)
        self.assertEqual(result["days_left"], 45)

    def test_expires_today_is_zero(self):
        result, _, _ = self._run({"notAfter": _cert_not_after(0)})
        self.assertEqual(result["days_left"], 0)

    def test_expired_cert_has_negative_days_left(self):
        result, _, _ = self._run({"notAfter": _cert_not_after(-3)})
        self.assertEqual(result["days_left"], -3)

    def test_custom_port_passed_to_create_connection(self):
        _, mock_create, _ = self._run({"notAfter": _cert_not_after(10)},
                                      port=8443)
        args, kwargs = mock_create.call_args
        self.assertEqual(args[0], ("example.com", 8443))
        self.assertIn("timeout", kwargs)

    def test_default_port_is_443(self):
        _, mock_create, _ = self._run({"notAfter": _cert_not_after(10)})
        args, _kwargs = mock_create.call_args
        self.assertEqual(args[0], ("example.com", 443))

    def test_server_hostname_passed_to_wrap_socket(self):
        _, _, mock_create_context = self._run(
            {"notAfter": _cert_not_after(10)})
        mock_context = mock_create_context.return_value
        _, kwargs = mock_context.wrap_socket.call_args
        self.assertEqual(kwargs.get("server_hostname"), "example.com")

    def test_connection_failure_raises_network_error_with_domain(self):
        mock_create, mock_create_context = _mock_tls(None)
        mock_create.side_effect = OSError("unreachable")
        with patch("clouds.cloudflare.socket.create_connection",
                   mock_create), \
             patch("clouds.cloudflare.ssl.create_default_context",
                   mock_create_context):
            with self.assertRaises(CloudflareError) as ctx:
                check_ssl_expiry("example.com")
        self.assertEqual(ctx.exception.kind, "network")
        self.assertIn("example.com", str(ctx.exception))
        # Do not expose raw socket details. 不裸露 socket 细节。
        self.assertNotIn("unreachable", str(ctx.exception))
        self.assertNotIn("Errno", str(ctx.exception))

    def test_ssl_error_raises_network_error(self):
        mock_create, mock_create_context = _mock_tls(None)
        mock_create.side_effect = ssl.SSLError("bad handshake")
        with patch("clouds.cloudflare.socket.create_connection",
                   mock_create), \
             patch("clouds.cloudflare.ssl.create_default_context",
                   mock_create_context):
            with self.assertRaises(CloudflareError) as ctx:
                check_ssl_expiry("example.com")
        self.assertEqual(ctx.exception.kind, "network")
        self.assertIn("example.com", str(ctx.exception))
        self.assertNotIn("bad handshake", str(ctx.exception))

    def test_missing_not_after_raises_api_error(self):
        with self.assertRaises(CloudflareError) as ctx:
            self._run({"subject": ((("commonName", "example.com"),),)})
        self.assertEqual(ctx.exception.kind, "api")
        self.assertIn("example.com", str(ctx.exception))

    def test_none_cert_raises_api_error(self):
        with self.assertRaises(CloudflareError) as ctx:
            self._run(None)
        self.assertEqual(ctx.exception.kind, "api")
        self.assertIn("example.com", str(ctx.exception))

    def test_unparseable_date_raises_api_error(self):
        with self.assertRaises(CloudflareError) as ctx:
            self._run({"notAfter": "not-a-date"})
        self.assertEqual(ctx.exception.kind, "api")
        self.assertIn("example.com", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
