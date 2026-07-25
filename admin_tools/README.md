# Mixcut AI 管理员授权工具

此目录不得进入用户发布 ZIP。

## 简易图形程序

双击 `MixcutAI-Keygen.exe`：

1. 从客户激活窗口复制机器码。
2. 粘贴到“客户机器码”。
3. 选择月卡、年卡或永久授权。
4. 点击“生成并复制”。
5. 将完整的 `MXLIC1...` 密钥发给客户粘贴激活。

签发机器绑定授权：

```powershell
python issue_license.py --machine-id MX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX --type month
python issue_license.py --machine-id MX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX --type year
python issue_license.py --machine-id MX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX --type permanent
```

月卡、年卡可以通过 `--expire-date YYYY-MM-DD` 指定到期日。输出的 `MXLIC1...` 字符串就是可离线验证的签名授权凭证。

## 密钥管理

- `keys/license_private_key.pem`：仅管理员保存，不上传用户端、不写入日志。
- 客户端只包含 Ed25519 公钥。
- 正式上线后建议将私钥迁移到授权服务器或 HSM/KMS，并记录签发、吊销、解绑和换机审计日志。
