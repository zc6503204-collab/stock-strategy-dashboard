# 参与贡献

欢迎改进策略实现、数据完整性、回放验证、界面和文档。

## 本地检查

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
node --check web/app.js
python3 scripts/check_no_secrets.py --history
```

策略变更需要说明数据需求、适用市场、触发与取消条件、止损止盈、最大持有时间，以及怎样防止未来数据泄漏。涉及真实行情的测试应使用合成数据或去标识化夹具。

提交中不得包含任何 API key、OAuth 令牌、账户号、真实持仓、数据库、日志、行情缓存或数据提供方安装包。不要把 `.local/` 内容复制到测试、截图、Issue 或 Pull Request。

这个项目用于研究、提醒和模拟验证。新增真实订单、撤单、改单或券商写权限不属于当前贡献范围。
