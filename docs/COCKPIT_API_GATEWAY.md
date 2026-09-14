# Cockpit Tools API Gateway

该网关把本机 Cockpit API（默认 `127.0.0.1:53417/v1`）包装成客户可访问的 `/v1` API。Cockpit 账号池仍由本机服务调度，网关不提供账号、OAuth 或管理接口。

## 启动

先在安全的进程环境变量中设置上游内部 API Key，不要写入仓库：

```powershell
$env:COCKPIT_UPSTREAM_API_KEY = "<Cockpit内部API Key>"
python cockpit_api_gateway.py --config "$env:LOCALAPPDATA\Cockpit Tools\gateway\customers.json"
```

默认监听 `0.0.0.0:53418`。正式公网使用时，在服务器侧用 HTTPS 反向代理转发到该端口，客户填写：`https://api.example.com/v1`。

## 客户Token

配置文件只保存 SHA-256，不保存可直接使用的客户Token：

```json
{
  "customers": [
    {"id": "customer_001", "token_sha256": "<sha256(token)>", "requests_per_minute": 60, "enabled": true}
  ]
}
```

请求使用：`Authorization: Bearer <customer-token>`。

访问日志写入 `%LOCALAPPDATA%\Cockpit Tools\gateway\access.jsonl`，只记录客户ID、Token指纹、模型、usage和HTTP状态，不记录原始Token、请求正文、账号密码或OAuth信息。

网关仅开放 `/healthz` 和 `/v1/*`，其他路径返回404；上游密钥只由网关进程读取并替换客户Token，不会回传给客户。不要直接把 Cockpit 管理端口暴露到公网。
