---
name: unified-search
title: 统一搜索服务 (Unified Search Service)
description: 整合多渠道 API 聚合、时效大模型搜索、多级级联全文深爬与本地缓存，提供解耦、高容错、防更新瘫痪的至尊搜索能力。
tags:
  - search
  - api-aggregation
  - cache
  - decouple
version: 2.3.0
author: Hermes AI
---

# 统一搜索服务 (Unified Search Service)

## 📌 架构与解耦说明
本搜索技能已实现与 Hermes Agent 代码库的**完全解耦**。
- **实体路径**：`~/.hermes/skills/research/unified-search/scripts/search_router.py` (高可用核心)
- **保护机制**：`/home/taiyizhenshui/.hermes/scripts/search_router.py` 仅作为自恢复加载存根（Stub），即便系统由于版本更新格式化了 scripts 路径，搜索能力依然毫发无损且能原地恢复，杜绝瘫痪。

---

## 🛠️ 三大黄金核心模块

### 1. 🌐 智能分流与消歧聚合搜索
- **特性**：合并了百度、Brave、Serper、SerpApi、DuckDuckGo 等搜索引擎。支持基于 SHA256 的本地热数据缓存（`SearchCache`，有效期由 `"search.cache_ttl"` 字段灵活控制）。
- **智能消歧重塑 (`QueryReformulator`)**：
  - 加载国外服务时，自动提取其中中文句子的专业核心词和英语代码部分，提高海外召回精准度。
  - **大牌科技词无冲突消歧**：搜“火山引擎、豆包、Kimi等”时，自动附加或关联强匹配。例如在与科技关联的场景中将“字节跳动 / 火山引擎”重塑为“火山引擎 火山方舟 大模型生态”，在底层彻底切断由于“火山岩浆地理”或“存储单位 Byte 字节”带来的低幼常识性干扰与国内弱分词引擎污染。

### 2. ⚡ 百度智能云千帆大模型搜索 MCP 双通道
- **首发端点**：`https://qianfan.baidubce.com/v2/tools/web-search/mcp` （支持 webSearch 工具调用，时效性直达 2026 年最新战报）。
- **自适应退避降级**：若千帆 MCP 端点抛出异常、配额耗尽、DNS 瘫痪或 Token 无效，**系统在 8 秒内迅速动作，直接静默平滑降阶至免费的百度爬虫 `BaiduSearchProvider` 分支**，保证检索 100% 畅通。

### 3. ⏳ 级联 Deep Research 深爬案头卷宗
- **方法**：`router.deep_research(query, max_results=3)`
- **流程**：先跑检索拿到黄金 Top 3 链接 -> 后台自发拉起三线程并发爬虫 -> 剥除网页 HTML/JS/CSS 垃圾结构 -> 重修汇聚成一份数万字包含深度内容的 Markdown **「Web Research Dossier (网络情报卷宗)」**，彻底跨越短摘要段的信息断崖。

---

## 💻 命令行与 Python 技术调用

### 1. 快速基础搜索
```python
import sys
sys.path.insert(0, '/home/taiyizhenshui/.hermes/scripts')
from search_router import SearchRouter

router = SearchRouter()

# 调用普通路由检索 (将自动消歧并优先使用千帆 MCP 通道)
results = router.search("火山引擎 MCP 最新更新", category="general", max_results=3)
for r in results:
    print(f"- {r.title} | Link: {r.url}")
```

### 2. 实战并发级联深爬 (Deep Research)
```python
# 启动多线程深爬，返回提纯后的 Markdown 知识卷宗
report = router.deep_research("火山引擎 MCP 大模型生态", max_results=2, use_cache=False)
if report.get("success"):
    print(report["dossier"])  # 包含清洗后的网页全文内容提要
```

---

## 🔒 凭证配置 (`auth.json`)
请于 `~/.hermes/auth.json` 处填入您的合法 API keys。
```json
{
  "credential_pool": {
    "custom:qianfan": [{"access_token": "YOUR_BCE_TOKEN"}],
    "serper": [{"access_token": "YOUR_SERPER_KEY"}],
    "serpapi": [{"access_token": "YOUR_SERPAPI_KEY"}],
    "tavily": [{"access_token": "YOUR_TAVILY_KEY"}]
  }
}
```
*注：Jina AI (海外端点) 因网络高频超时目前处于默认停用状态。*
*注：Jina AI (海外端点) 因网络高频超时目前处于默认停用状态。*
