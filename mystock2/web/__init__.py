"""只读 Web（透视 Lens）：Flask 应用、视图框架、公共约定。入口见 `mystock2.web.app.create_app`。

约束（AGENTS.md、实施方案 §3.4）：只用只读连接；不 import forecast/collectors/assistant；只监听回环地址。
"""
