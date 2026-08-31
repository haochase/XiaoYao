# 参与贡献

感谢参与 XiaoYao。提交改动前，请先说明要解决的问题和预期行为，较大的功能建议先创建
Issue 对齐接口边界。

## 本地开发

```powershell
Set-Location gateway
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python -m pytest tests
```

## 提交要求

- 一个 Pull Request 只处理一个清晰主题，并补充相应测试。
- 保持 ESP32 协议、模型适配和业务模块之间的边界。
- 不提交 `.env`、API Key、设备令牌、真实用户数据、日志、数据库或固件备份。
- 使用现有格式化与测试命令，确保 `git diff --check` 通过。
- 对尚未在真实硬件或昇腾环境运行的能力，明确标记为未验证。

提交即表示你同意以项目的 MIT License 发布贡献内容。
