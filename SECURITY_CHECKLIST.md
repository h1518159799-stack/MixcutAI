# Mixcut AI v1.2.0 授权与发布安全检查

## 结论

用户 EXE 只包含授权验证公钥，不包含签发私钥、管理员脚本、服务器管理密钥、固定 AI API Key、数据库密码或测试数据库。授权真相已从 SQLite 迁移到 Ed25519 签名 `license.dat`。

## 密钥与敏感信息

| 检查项 | 结果 | 说明 |
|---|---|---|
| 卡密生成私钥进入 EXE | 通过 | EXE 不含 PEM 私钥头，也不含管理员私钥原始字节。 |
| 服务器管理密钥进入 EXE | 通过 | 未发现管理密钥字段或固定管理凭证。 |
| 固定公共 AI API Key | 通过 | 未发现 `sk-*` 模式；`default_config.json` 无 API Key。 |
| 数据库管理密码 | 通过 | SQLite 无管理密码；EXE 未发现 `db_password`/`database_password` 固定值。 |
| 客户运行时填写的 AI Key | 隔离 | 仅存用户本地 SQLite，不进入构建产物。 |

扫描证据：`qa/security-scan-results.json`、`qa/pyinstaller-license-contents.txt`。

## 授权信任链

1. 管理员工具或授权服务器持有 Ed25519 私钥。
2. 客户端仅内置公钥。
3. 签名载荷包含：`license_id`、`product_id`、`type`、`machine_id`、`issued_at`、`expire_date`、`nonce`、`max_devices`、`transfer_policy`。
4. 激活时先校验签名、产品、机器码、时间和授权类型，再原子写入 `data/license.dat`。
5. 启动时重新校验 `license.dat`；SQLite `license_keys` 只作为状态缓存。
6. 删除 `license.dat` 后，即使 SQLite 中仍为 `active`，软件也返回 `missing`。

## 有效期与时间安全

- 月卡、年卡有效期位于签名载荷内，修改本地数据库 `expire_date` 无效。
- 记录 `license_clock.json` 与 HKCU Registry 双检查点。
- 当前时间早于签发时间或历史最大时间超过 5 分钟，拒绝启动并要求联网校准。
- 配置授权服务器后，启动调用 `/api/v1/licenses/verify`，服务器可吊销并返回新签名凭证。
- 月卡/年卡服务器不可达时受 `license_offline_grace_days` 限制。
- 永久授权可离线运行，但仍校验签名、机器码和回拨。

## 解绑与换机预留

- `POST /api/v1/licenses/deactivate`
- `POST /api/v1/licenses/transfer`
- 换机请求携带源机器、目标机器、产品和签名授权；服务器审批后必须签发绑定新机器的新凭证。
- 默认载荷：`max_devices=1`、`transfer_policy=server_approval`。

接口定义：`LICENSE_SERVER_API.md`。

## 发布包隔离

用户 ZIP allowlist：

- `MixcutAI.exe`
- `MixcutUpdater.exe`
- `default_config.json`

构建脚本会拒绝以下内容：

- `.py`、`.db`、`.log`、`.env`、`.key`、`.pem`
- `license_admin`、`production_test`、`test_*`
- 名称含 `admin` 的文件

PyInstaller 递归归档检查只发现客户端 `license_module`，未发现 `issue_license`、`license_admin`、私钥或测试模块。

## 管理员工具隔离

- 工作目录：`admin_tools/`
- 管理员发行目录：`admin_release/MixcutAI-License-Admin/`
- 管理员 ZIP：`admin_release/MixcutAI-License-Admin-v1.2.0.zip`
- 私钥：`admin_tools/keys/license_private_key.pem`
- 用户 ZIP 不包含上述任何文件。

正式运营时，私钥应迁移到授权服务器的 KMS/HSM；管理员 ZIP 只用于当前离线发行与 QA，不发送给最终用户。

## 尚需运营侧完成

- 配置正式 HTTPS 授权服务器和吊销数据库。
- 将 Ed25519 私钥迁入 KMS/HSM，并停止通过文件分发私钥。
- 购买 Windows Authenticode 代码签名证书，对 EXE 和 ZIP 发布哈希签名。
- 建立卡密签发、激活、失败、解绑、换机、吊销审计日志与管理员双因素认证。
