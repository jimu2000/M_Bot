# IAM 控制台密码重置排查

M_Bot 的 Web 面板和 Telegram 机器人共用 `oci.iam.create_or_reset_ui_password()`，调用 OCI CLI 官方命令：

```bash
oci iam user ui-password create-or-reset --user-id <USER_OCID>
```

## 常见情况

- `NotAuthorizedOrNotFound`：OCI 有意使用模糊错误码。检查当前 OCI API 签名用户是否属于 `Administrators` 组，或其所在组是否具有 tenancy 级用户管理权限。可在租户根 compartment 创建策略：`Allow group <组名> to manage users in tenancy`。仅有某个 compartment 的权限通常不够。
- 目标用户必须存在且处于 `ACTIVE`。被停用的用户不能通过此流程重置。
- 支持 Identity Domains 的租户可能不会返回临时密码，而是给用户邮箱发送重置链接；应检查收件箱和垃圾邮件。
- 若账号其他 IAM 操作正常、只有密码重置失败，检查目标用户是否由 Identity Domain 管理，并确认 OCI 配置中的主区域和 API 签名密钥正确。

## 临时密码安全

经典 IAM 用户的临时密码只在成功响应中返回一次。首次登录时用户必须修改；未在 OCI 规定期限内修改的临时密码会过期。不要把临时密码粘贴到公共群聊、日志或工单中。

## 重要限制

面板和机器人不能绕过 OCI IAM 的服务端授权。若当前 API 用户没有 tenancy 级权限，必须由 OCI 管理员调整 IAM 组/策略，或直接在 OCI Console / Identity Domain 管理界面重置。M_Bot 的 `Group_for_Api_used` 专用 API 用户策略现已加入 `manage users in tenancy`，以支持面板/机器人中的用户创建、删除、API Key 与控制台密码管理。此权限可管理租户内所有 IAM 用户及其凭据，属于高权限；升级前请确认你接受该权限范围。M_Bot 不会尝试用目标用户凭据代替管理员执行操作。
