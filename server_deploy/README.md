# MixcutAI 云端授权服务器

## 地址

- 管理后台：`https://47-107-104-106.sslip.io/admin`
- 健康检查：`https://47-107-104-106.sslip.io/health`
- 更新清单：`https://47-107-104-106.sslip.io/updates/update.json`

## 客户端 API

```text
POST /api/license/activate
POST /api/license/check
POST /api/license/deactivate
```

旧版 `/api/v1/licenses/*` 路径继续保留，避免旧客户端失效。请求包含：

```json
{
  "product_id": "mixcut-ai-windows",
  "app_version": "1.2.2",
  "machine_id": "MX-...",
  "device_name": "DESKTOP-XXX",
  "os_info": "Windows-10-..."
}
```

激活额外提交 `key_code`，校验和解绑提交 `license_token`。

## 数据库

数据库：`/var/lib/mixcut/licenses.db`

- `customers`：客户资料
- `license_keys`：卡密哈希、授权类型、期限和状态
- `devices`：设备绑定、系统、版本、IP和最近在线
- `activation_logs`：激活、校验、解绑及失败记录

升级前备份：`/var/backups/mixcut/licenses-before-cloud-*.db`

## 运维

```bash
systemctl status mixcut-license
systemctl status caddy
journalctl -u mixcut-license -n 100 --no-pager
```

生成卡密：

```bash
runuser -u mixcut -- /opt/mixcut/venv/bin/python /opt/mixcut/server/mixcut_admin.py create --type month --count 1
runuser -u mixcut -- /opt/mixcut/venv/bin/python /opt/mixcut/server/mixcut_admin.py create --type year --count 10 --max-devices 2
```

重置后台管理员密码：

```bash
runuser -u mixcut -- /opt/mixcut/venv/bin/python /opt/mixcut/server/mixcut_admin.py admin-user --username admin --password-file /secure/password.txt
```

私钥：`/etc/mixcut/license_private_key.pem`，权限 `0640 root:mixcut`；客户端发布包不得包含该文件。

