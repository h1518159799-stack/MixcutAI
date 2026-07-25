# 备份内容清单

包含：Python 全部源码、React/Vite 前端源码与锁文件、`platforms/`、`publishers/`、数据库 Schema/迁移代码、`tests/`、构建发布脚本、品牌资源、默认音效、配置模板、项目文档、小型测试素材、脱敏开发数据库、完整 `.git` 历史。

关键目录：

- `ui-commercial/`：前端源码（不含 node_modules/dist）
- `platforms/`、`publishers/`：平台适配器
- `tests/`：自动化测试
- `backup_data_sanitized/`：脱敏数据库、Schema、脱敏摘要
- `backup_tools/`：数据库脱敏工具
- `test_assets/`：小型测试视频和封面
- `brand/`、`default_sfx/`、`config/`：必要资源与配置
- `admin_tools/`、`server_deploy/`、`server_update/`：开发侧工具与服务源码（不含生成物和秘密）

压缩包内的 `BACKUP_FILE_MANIFEST.txt` 是逐文件清单。
