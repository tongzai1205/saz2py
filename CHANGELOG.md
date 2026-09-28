# Changelog

## v1.0.0 — 2026-09-28

首个正式版本。Fiddler `.saz` 抓包 → **可回放 Python 脚本 / pytest 用例 / Postman 集合**，一条命令完成。

### 核心能力

- **零依赖单文件**：`saz2py.py` 本体纯标准库（3.9+），单文件拷走即用；亦支持 `pip install saz2py`
- **自动去重 + 噪音过滤**：同接口只保留最新参数；自动剔除 OPTIONS 预检、SPA 页面路由、Vite/Webpack 静态资源
- **链式变量**：自动识别「接口 A 响应中的 id → 接口 B 请求引用」，pytest 生成 `ctx` 提取与传递（`ctx.get(name, 抓包原值)` 兜底，链条永不断），Postman 生成集合变量 `{{id}}` 与自动 set 的测试脚本
- **业务断言自动生成**：从抓包响应推断成功包装（`code`/`status`/`success`/`data`），pytest 与 Postman 同步生成——解决「HTTP 200 包装业务 401，状态码断言全绿却已失效」的经典盲区
- **pytest 模式**：`--pytest` 生成标准用例，`requests.Session` fixture（连接复用、Cookie 保持）、按抓包顺序执行、可直接进 CI
- **Postman Collection v2.1**：Import 即用，变量与断言一并生成
- **损坏恢复模式**：`.saz` 导出中断/复制不全导致 zip 中央目录丢失时，自动逐条目扫描恢复
- **兼容两种内部命名**：经典 Fiddler（`raw/_c.txt001`）与 Fiddler Everywhere 风格（`raw/001_c.txt`）

### 实测

- 53MB 截断抓包（15402 会话）恢复出 112 个业务接口并生成 pytest 用例
- 真实系统回放：状态码断言 129/129 通过的同时，业务断言揪出 110 个登录态失效接口

### 已知限制

- 仅支持 Fiddler `.saz`（HAR / Charles 在 Roadmap）
- 业务断言基于抓包时的响应结构推断，接口契约变更后需人工复核
- 相同接口保留最后一次抓包的参数
