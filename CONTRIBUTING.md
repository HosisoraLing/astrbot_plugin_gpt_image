# Contributing

感谢贡献。提交前请确认：

1. 不要提交 API Key、OAuth 凭据、socket、生成图片、session 映射或本地配置。
2. 运行 `python -m py_compile main.py bridge/codex_image_bridge.py`。
3. 保持 Codex 后端的搜索与 session 续接行为，不要把长任务重新放回 AstrBot 60 秒 Tool 调用中。
4. 新增配置时同步更新 `_conf_schema.json` 和 README。

Issue 和 Pull Request 请说明 AstrBot 版本、后端类型、复现步骤及相关日志；请先删除提示词中的敏感信息。
