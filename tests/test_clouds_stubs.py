#!/usr/bin/env python3
"""clouds/ 抽象接口与 6 个厂商 stub 的单元测试（不碰真实 API）。

运行：python3 -m unittest tests.test_clouds_stubs -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clouds import (AWSProvider, AzureProvider, CloudProvider, DOProvider,
                    GCPProvider, SolusVMProvider, VirtFusionProvider)

# Test data always uses documentation-reserved values; no real IP / OCID / Token / key ever appears. 测试数据一律用文档保留值，绝不出现真实 IP / OCID / Token / 密钥。
FAKE_INSTANCE_ID = "test-instance-id"
FAKE_PUBLIC_IP = "203.0.113.10"
FAKE_HOSTNAME = "example.com"
# AWS official documentation example keys (not real credentials; see the AWS docs examples). AWS 官方文档示例密钥（非真实凭据，见 AWS 文档示例）。
FAKE_AWS_CREDS = {
    "access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "region": "us-east-1",
}
FAKE_GCP_CREDS = {
    "credentials_path": "/tmp/fake-service-account.json",
    "project": "fake-project",
    "zone": "us-central1-a",
}
FAKE_AZURE_CREDS = {
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "tenant_id": "00000000-0000-0000-0000-000000000000",
    "client_id": "00000000-0000-0000-0000-000000000000",
    "client_secret": "fake-client-secret",
}
FAKE_DO_CREDS = {"token": "dop_v1_fake_token_example"}
FAKE_SOLUSVM_CREDS = {
    "api_url": "https://example.com/api/client",
    "api_key": "fake-api-key",
    "api_hash": "fake-api-hash",
}
FAKE_VF_CREDS = {
    "api_url": "https://example.com/api/v1",
    "api_token": "fake-vf-token",
}

# The 11 abstract method names (matching the CloudProvider interface list). 11 个抽象方法名（与 CloudProvider 的接口清单一致）。
EXPECTED_ABSTRACT_METHODS = {
    "list_instances",
    "get_instance",
    "create_instance",
    "start_instance",
    "stop_instance",
    "restart_instance",
    "terminate_instance",
    "list_public_ips",
    "change_public_ip",
    "list_volumes",
    "get_quota",
}

# The parameters one call of each method needs (sampling must at least cover list_instances / get_instance / create_instance / terminate_instance / list_public_ips / get_quota; here all 11 methods are covered). 每个方法一次调用所需的参数（抽样至少覆盖 list_instances / get_instance / create_instance / terminate_instance / list_public_ips / get_quota；这里全覆盖 11 个方法）。
METHOD_CALLS = [
    ("list_instances", (), {}),
    ("get_instance", (FAKE_INSTANCE_ID,), {}),
    ("create_instance", ("test-instance", "test-shape", "test-image"),
     {"hostname": FAKE_HOSTNAME, "public_ip": FAKE_PUBLIC_IP}),
    ("start_instance", (FAKE_INSTANCE_ID,), {}),
    ("stop_instance", (FAKE_INSTANCE_ID,), {}),
    ("restart_instance", (FAKE_INSTANCE_ID,), {}),
    ("terminate_instance", (FAKE_INSTANCE_ID,), {}),
    ("list_public_ips", (), {}),
    ("change_public_ip", (FAKE_INSTANCE_ID,), {}),
    ("list_volumes", (), {}),
    ("get_quota", (), {}),
]


class TestCloudProviderBase(unittest.TestCase):
    """抽象接口完整性。"""

    def test_abstract_methods_complete(self):
        """CloudProvider 的抽象方法集合与预期清单一致。"""
        self.assertEqual(set(CloudProvider.__abstractmethods__),
                         EXPECTED_ABSTRACT_METHODS)

    def test_cannot_instantiate_abstract_base(self):
        """抽象基类不能直接实例化。"""
        with self.assertRaises(TypeError):
            CloudProvider()

    def test_concrete_subclass_can_implement_interface(self):
        """实现全部 11 个方法的子类可以实例化，且返回约定形状。"""

        class DummyProvider(CloudProvider):
            PROVIDER_NAME = "dummy"
            display_name = "Dummy"

            def list_instances(self):
                return [{"id": FAKE_INSTANCE_ID, "name": "test-instance",
                         "shape": "test-shape", "state": "RUNNING",
                         "public_ip": FAKE_PUBLIC_IP,
                         "private_ip": "10.0.0.1",
                         "region": "test-region"}]

            def get_instance(self, instance_id):
                return self.list_instances()[0]

            def create_instance(self, name, shape, image, **kwargs):
                return {"id": FAKE_INSTANCE_ID, "name": name,
                        "shape": shape, "state": "PROVISIONING",
                        "public_ip": None, "private_ip": None,
                        "region": "test-region"}

            def start_instance(self, instance_id):
                return {"id": instance_id, "state": "RUNNING"}

            def stop_instance(self, instance_id):
                return {"id": instance_id, "state": "STOPPED"}

            def restart_instance(self, instance_id):
                return {"id": instance_id, "state": "RUNNING"}

            def terminate_instance(self, instance_id):
                return True

            def list_public_ips(self):
                return [{"ip": FAKE_PUBLIC_IP, "id": "test-ip-id",
                         "assigned_to": FAKE_INSTANCE_ID}]

            def change_public_ip(self, instance_id):
                return {"ip": FAKE_PUBLIC_IP, "id": "test-ip-id"}

            def list_volumes(self):
                return [{"id": "test-volume-id", "name": "test-volume",
                         "size_gb": 50, "state": "AVAILABLE",
                         "attached_to": None}]

            def get_quota(self):
                return {"vcpu_used": 2, "vcpu_limit": 4}

        provider = DummyProvider()
        self.assertEqual(provider.PROVIDER_NAME, "dummy")
        instances = provider.list_instances()
        for key in ("id", "name", "shape", "state", "public_ip",
                    "private_ip", "region"):
            self.assertIn(key, instances[0])
        self.assertTrue(provider.terminate_instance(FAKE_INSTANCE_ID))
        self.assertEqual(provider.get_quota()["vcpu_limit"], 4)


def _make_stub_tests(provider_cls, provider_name, display_name, creds):
    """为一个 stub 类生成测试类（实例化 / 属性 / 方法抛错）。"""

    class StubTests(unittest.TestCase):
        def setUp(self):
            self.provider = provider_cls()

        def test_instantiates_without_credentials(self):
            """无凭据也能实例化（只保存配置，不做真实连接）。"""
            self.assertIsInstance(self.provider, provider_cls)

        def test_instantiates_with_credentials(self):
            """传凭据参数实例化成功，且配置被保存下来。"""
            provider = provider_cls(**creds)
            self.assertIsInstance(provider, provider_cls)
            for key, value in creds.items():
                self.assertEqual(provider.config.get(key), value)

        def test_provider_name_and_display_name(self):
            """PROVIDER_NAME 与 display_name 正确。"""
            self.assertEqual(provider_cls.PROVIDER_NAME, provider_name)
            self.assertEqual(provider_cls.display_name, display_name)

        def test_is_cloud_provider_subclass(self):
            """是 CloudProvider 的子类。"""
            self.assertTrue(issubclass(provider_cls, CloudProvider))
            self.assertIsInstance(self.provider, CloudProvider)

        def test_all_methods_raise_not_implemented(self):
            """全部 11 个方法都抛 NotImplementedError，且消息含厂商与方法名。"""
            for method_name, args, kwargs in METHOD_CALLS:
                with self.subTest(method=method_name):
                    with self.assertRaises(NotImplementedError) as ctx:
                        getattr(self.provider, method_name)(*args, **kwargs)
                    message = str(ctx.exception)
                    self.assertIn(display_name, message)
                    self.assertIn(method_name, message)

    StubTests.__name__ = f"Test{provider_cls.__name__}"
    StubTests.__qualname__ = StubTests.__name__
    return StubTests


TestAWSProvider = _make_stub_tests(
    AWSProvider, "aws", "AWS", FAKE_AWS_CREDS)
TestGCPProvider = _make_stub_tests(
    GCPProvider, "gcp", "GCP", FAKE_GCP_CREDS)
TestAzureProvider = _make_stub_tests(
    AzureProvider, "azure", "Azure", FAKE_AZURE_CREDS)
TestDOProvider = _make_stub_tests(
    DOProvider, "do", "DigitalOcean", FAKE_DO_CREDS)
TestSolusVMProvider = _make_stub_tests(
    SolusVMProvider, "solusvm", "SolusVM", FAKE_SOLUSVM_CREDS)
TestVirtFusionProvider = _make_stub_tests(
    VirtFusionProvider, "vf", "VirtFusion", FAKE_VF_CREDS)


if __name__ == "__main__":
    unittest.main()
