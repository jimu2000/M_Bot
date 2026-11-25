#!/usr/bin/env python3
"""统一云厂商抽象接口：所有云厂商模块都要实现 CloudProvider。

方法名与 oci/instance.py、oci/network.py 的函数名对齐（list_instances、
get_instance、start/stop/restart/terminate、list_public_ips、
change_public_ip、list_volumes、get_quota），但返回统一的 snake_case
字段（id/name/shape/state/public_ip/private_ip/region），各厂商实现
负责把自家字段归一化成这个形状。

OCI 本身继续走 oci/ 模块（调官方 OCI CLI），不在 clouds/ 里再包一层。
"""

# ================= 配置区（改这里） =================
# Credential injection (hard rule): credentials for every cloud come only from environment variables — never hardcoded in code and never written into the repo. 凭据注入（硬规则）：各云凭据一律用环境变量注入，绝不硬编码进代码，也不写进仓库。
# Each cloud's environment variable names are listed in its stub file header (e.g. aws.py uses AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION). 各云的环境变量名见对应 stub 文件头（如 aws.py 用 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION）。
# __init__ only stores the config and makes no real connection; missing credentials do not error — the method call decides how to fail. __init__ 只保存配置，不做真实连接；缺凭据也不报错，调用方法时才由具体实现决定如何报错。
# =====================================================

import abc


class CloudProvider(abc.ABC):
    """云厂商统一接口。

    子类必须覆盖的类属性：
        PROVIDER_NAME: 厂商标识，如 "aws"（小写，用于配置与路由）。
        display_name: 展示名，如 "AWS"（用于日志与界面）。
    """

    PROVIDER_NAME = None
    display_name = None

    def __init__(self, **config):
        """保存配置，不做真实连接。

        参数：
            **config: 凭据与区域等配置（各子类可用具名参数接收，
                缺凭据也不报错，调用方法时才报错）。
        """
        self.config = dict(config)

    @abc.abstractmethod
    def list_instances(self):
        """列出当前账号/区域下的实例。

        返回：
            list[dict]，每项含 id / name / shape / state /
            public_ip / private_ip / region。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_instance(self, instance_id):
        """查单个实例详情。

        参数：
            instance_id: 实例 ID。
        返回：
            dict，含 id / name / shape / state / public_ip /
            private_ip / region（可附带厂商特有字段）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def create_instance(self, name, shape, image, **kwargs):
        """创建实例。

        参数：
            name: 实例名。
            shape: 规格（如 AWS 的实例类型、DO 的 size slug）。
            image: 镜像 ID 或镜像名。
            **kwargs: 厂商特有参数（如区域、SSH key、启动脚本、
                磁盘大小、是否分配公网 IP）。
        返回：
            dict，新实例的详情（同 get_instance 的形状）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def start_instance(self, instance_id):
        """启动实例。

        参数：
            instance_id: 实例 ID。
        返回：
            dict，操作后的实例详情（同 get_instance 的形状）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def stop_instance(self, instance_id):
        """停止实例。

        参数：
            instance_id: 实例 ID。
        返回：
            dict，操作后的实例详情（同 get_instance 的形状）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def restart_instance(self, instance_id):
        """重启实例。

        参数：
            instance_id: 实例 ID。
        返回：
            dict，操作后的实例详情（同 get_instance 的形状）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def terminate_instance(self, instance_id):
        """终止（删除）实例。

        参数：
            instance_id: 实例 ID。
        返回：
            bool，True 表示终止请求已接受。
        注意：
            与 OCI 模块的硬规则一致：终止时默认同时删除引导卷，
            不留着继续扣钱；确需保留引导卷的厂商用 kwargs 显式指定。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def list_public_ips(self):
        """列出账号下的公网 IP（保留 IP / 弹性 IP）。

        返回：
            list[dict]，每项含 ip / id / assigned_to（绑定的实例 ID，
            未绑定为 None）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def change_public_ip(self, instance_id):
        """给实例换一个新的公网 IP。

        参数：
            instance_id: 实例 ID。
        返回：
            dict，含新 IP（ip）与 IP 资源 ID（id）等信息。
        注意：
            换 IP 后旧 IP 立即释放无法找回，指向旧 IP 的 DNS 与
            客户端配置必须同步更新；换 IP 瞬间会断开旧 IP 上的连接。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def list_volumes(self):
        """列出账号下的磁盘卷。

        返回：
            list[dict]，每项含 id / name / size_gb / state /
            attached_to（挂载的实例 ID，未挂载为 None）。
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_quota(self):
        """查配额 / 免费额度使用情况。

        返回：
            dict，如 {"vcpu_used": 2, "vcpu_limit": 4,
            "memory_gb_used": 12, "memory_gb_limit": 24,
            "instances_used": 1, "instances_limit": 2}；
            各云能查到的维度不同，只返回该云实际支持的字段。
        """
        raise NotImplementedError
