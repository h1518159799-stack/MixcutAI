# 授权服务器预留接口

客户端请求：

```http
POST /api/v1/licenses/activate
Content-Type: application/json
```

```json
{
  "key_code": "MX-XXXXX-XXXXX-XXXXX-XXXXX",
  "machine_id": "MX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX",
  "product_id": "mixcut-ai-windows",
  "app_version": "1.2.0"
}
```

成功响应：

```json
{
  "ok": true,
  "data": {
    "license_token": "MXLIC1.signed-payload.ed25519-signature"
  }
}
```

`license_token` 是 Ed25519 签名授权文档，载荷必须包含 `license_id`、`product_id`、`type`、`machine_id`、`issued_at`、`expire_date`、`nonce`。

失败响应：

```json
{
  "ok": false,
  "error": "卡密无效或已绑定其他设备"
}
```

正式服务器应保存卡密状态、设备绑定、激活次数、吊销状态与审计日志；客户端只缓存服务器返回的授权状态。

## 启动校验

```http
POST /api/v1/licenses/verify
```

服务器返回 `status=active` 和最新签名 `license_token`。月卡和年卡断网时只允许配置的宽限期；永久签名授权可以离线运行，但仍执行机器码、签名和时钟回拨校验。

## 解绑与换机预留

- `POST /api/v1/licenses/deactivate`：解绑当前机器。
- `POST /api/v1/licenses/transfer`：提交源机器、目标机器和授权凭证，由服务器审批并签发绑定新机器的新凭证。
