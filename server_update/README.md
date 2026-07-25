# Update Server Files

只需要把新版 `MixcutAI.exe` 和 `update.json` 放到 HTTPS 静态文件服务器。

`update.json` 由 `build_release.ps1` 自动生成 SHA256。上传前必须把 `download_url` 改为真实 HTTPS 地址，并且最后上传 JSON。

此目录属于发行方，不进入用户 ZIP。
