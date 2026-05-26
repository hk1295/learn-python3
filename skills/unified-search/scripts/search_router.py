#!/home/taiyizhenshui/.hermes/hermes-agent/venv/bin/python3
# -*- coding: utf-8 -*-
"""
智能搜索路由器 - 多提供商自动切换和故障转移

支持百度搜索（中文优先）、Brave Search、Tavily Search 等多提供商，
实现自动故障转移和搜索统计。
"""

from __future__ import annotations

import json
import logging
import time
import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("search_router")

# ============================================================
# 搜索缓存管理器
# ============================================================


class SearchCache:
    """文件-based JSON 缓存管理器"""

    CACHE_DIR = Path.home() / ".hermes" / "cache"
    CACHE_FILE = CACHE_DIR / "search_cache.json"
    CACHE_TTL_SECONDS = 3600  # 缓存有效期: 1小时

    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS):
        self.ttl = ttl_seconds
        self._cache: dict = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """从文件加载缓存"""
        if not self.CACHE_FILE.exists():
            return
        try:
            with open(self.CACHE_FILE, 'r', encoding='utf-8') as f:
                self._cache = json.load(f)
            logger.debug("搜索缓存已加载: %d 条目", len(self._cache))
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("加载搜索缓存失败: %s, 将使用空缓存", e)
            self._cache = {}

    def _save_cache(self) -> None:
        """保存缓存到文件"""
        try:
            self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(self.CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            logger.debug("搜索缓存已保存: %d 条目", len(self._cache))
        except IOError as e:
            logger.warning("保存搜索缓存失败: %s", e)

    def _make_key(self, query: str, max_results: int) -> str:
        """生成缓存键: 使用 query + max_results 的 hash"""
        key_str = f"{query.strip().lower()}:{max_results}"
        return hashlib.sha256(key_str.encode('utf-8')).hexdigest()[:32]

    def get(self, query: str, max_results: int) -> Optional[list[dict]]:
        """获取缓存结果，如果存在且未过期返回结果，否则返回 None"""
        key = self._make_key(query, max_results)
        entry = self._cache.get(key)
        if not entry:
            return None

        # 检查是否过期
        cached_time = entry.get("_cached_at", 0)
        if time.time() - cached_time > self.ttl:
            logger.debug("缓存已过期: key=%s", key[:8])
            del self._cache[key]
            self._save_cache()
            return None

        logger.debug("缓存命中: key=%s query='%s'", key[:8], query)
        return entry.get("results")

    def set(self, query: str, max_results: int, results: list[SearchResult]) -> None:
        """保存搜索结果到缓存"""
        key = self._make_key(query, max_results)
        # 将 SearchResult 对象转换为字典
        results_dict = [asdict(r) for r in results]
        self._cache[key] = {
            "query": query,
            "max_results": max_results,
            "results": results_dict,
            "_cached_at": time.time(),
        }
        self._save_cache()
        logger.debug("缓存已保存: key=%s query='%s' results=%d", key[:8], query, len(results))

    def clear(self) -> None:
        """清空所有缓存"""
        self._cache = {}
        self._save_cache()
        logger.info("搜索缓存已清空")


def load_api_key_from_auth(provider_name: str) -> Optional[str]:
    """从 ~/.hermes/auth.json 加载 API key"""
    auth_file = Path.home() / ".hermes" / "auth.json"
    if not auth_file.exists():
        logger.debug("auth.json 不存在: %s", auth_file)
        return None
    
    try:
        with open(auth_file, 'r') as f:
            auth = json.load(f)
        
        # 检查 credential_pool
        credential_pool = auth.get('credential_pool', {})
        
        # 直接匹配 provider_name
        if provider_name in credential_pool:
            creds = credential_pool[provider_name]
            if creds and isinstance(creds, list) and creds[0]:
                api_key = creds[0].get('access_token')
                if api_key:
                    logger.debug("从 auth.json 加载 %s API key 成功", provider_name)
                    return api_key
        
        # 尝试带前缀的键名（如 "brave" 可能存为 "brave_search"）
        for key in credential_pool.keys():
            if provider_name in key.lower():
                creds = credential_pool[key]
                if creds and isinstance(creds, list) and creds[0]:
                    api_key = creds[0].get('access_token')
                    if api_key:
                        logger.debug("从 auth.json 加载 %s (键: %s) API key 成功", provider_name, key)
                        return api_key
        
        logger.debug("auth.json 中未找到 %s 的凭证", provider_name)
        return None
        
    except json.JSONDecodeError as e:
        logger.warning("auth.json 格式错误: %s", e)
        return None
    except Exception as e:
        logger.warning("读取 auth.json 失败: %s", e)
        return None


# ============================================================
# 数据模型
# ============================================================


@dataclass
class SearchResult:
    """搜索结果项"""
    title: str
    url: str = ""
    summary: str = ""
    source: str = ""


@dataclass
class ProviderStats:
    """提供商统计"""
    requests: int = 0
    success: int = 0
    fail: int = 0
    total_time: float = 0.0


@dataclass
class SearchStats:
    """搜索统计汇总"""
    total: int = 0
    success: int = 0
    fail: int = 0
    providers: dict[str, ProviderStats] = field(default_factory=dict)
    fallback_chain: list[dict] = field(default_factory=list)

    def record(self, provider: str, ok: bool, duration: float) -> None:
        self.total += 1
        if ok:
            self.success += 1
        else:
            self.fail += 1
        if provider not in self.providers:
            self.providers[provider] = ProviderStats()
        ps = self.providers[provider]
        ps.requests += 1
        if ok:
            ps.success += 1
        else:
            ps.fail += 1
        ps.total_time += duration

    def record_fallback(self, provider: str, category: str, error: str) -> None:
        self.fallback_chain.append({
            "provider": provider,
            "category": category,
            "error": error,
        })

    def summary(self) -> str:
        lines = [
            f"搜索统计: 总计={self.total}, 成功={self.success}, 失败={self.fail}",
        ]
        for name, ps in sorted(self.providers.items()):
            avg = ps.total_time / ps.requests if ps.requests else 0
            lines.append(
                f"  {name}: 请求={ps.requests}, 成功={ps.success}, "
                f"失败={ps.fail}, 平均耗时={avg:.2f}s"
            )
        if self.fallback_chain:
            lines.append("故障转移记录:")
            for fb in self.fallback_chain:
                lines.append(f"  [{fb['category']}] {fb['provider']}: {fb['error']}")
        return "\n".join(lines)


# ============================================================
# 搜索提供商基类
# ============================================================


class BaseSearchProvider(ABC):
    """搜索提供商抽象基类"""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.logger = logging.getLogger(f"search_router.{name}")

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        ...


# ============================================================
# Baidu API 搜索提供商（使用百度 AI 千帆官方 MCP 大模型搜索 API ＋ 智能爬虫双通道降级）
# ============================================================


class BaiduAPISearchProvider(BaseSearchProvider):
    """
    智能百度双通道千帆大模型搜索器 (取长补短，纳百家)
    
    1. 首选百度千帆官方大模型搜索 MCP 接口 (webSearch) 进行高精度、高时效性的智能检索。
    2. 若官方 MCP 接口因 DNS 异常、配额耗尽或 Token 无效，
       自动无缝切换到免 Key 的爬虫通道 (BaiduSearchProvider)，保证中文检索 100% 绝不断联。
    """

    # 升级：采用百度千帆官方最新的 V2 MCP 大模型检索端点
    BASE_URL = "https://qianfan.baidubce.com/v2/tools/web-search/mcp"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        # 自动获取千帆凭证
        self.api_key = self._get_api_key_silent()
        # 实例化内置的免 Key 爬虫作为底座保障 (纳百家)
        self.crawler = BaiduSearchProvider("baidu_backup_crawler", config)

    def _get_api_key_silent(self) -> Optional[str]:
        """静默获取千帆 API 凭证"""
        api_key = self.config.get("api_key", "")
        if not api_key:
            api_key = load_api_key_from_auth("custom:qianfan")
        return api_key if api_key else None

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not self.api_key:
            self.logger.info("ℹ️ 官方千帆 MCP Credential 未配置，自适应启用 MCP 式免 Key 爬虫通道...")
            return self.crawler.search(query, max_results)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        # 按照千帆 V2 webSearch 标准协议构建 JSON-RPC 载荷
        payload = {
            "method": "tools/call",
            "params": {
                "name": "webSearch",
                "arguments": {
                    "query": query,
                    "count": min(max_results, 50)
                }
            },
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) % 1000000
        }
        
        try:
            self.logger.debug("尝试调用官方百度千帆 AI 大模型搜索 MCP...")
            resp = requests.post(
                self.BASE_URL,
                headers=headers,
                json=payload,
                timeout=8  # 官方 MCP 处理通常在 2s 内
            )
            resp.raise_for_status()
            data = resp.json()
            
            # 解析千帆返回的 JSON-RPC 成功内容
            if "error" in data:
                error_msg = data["error"].get("message", "Unknown standard MCP Error")
                raise RuntimeError(f"Qianfan MCP internal error: {error_msg}")
                
            results: list[SearchResult] = []
            mcp_result = data.get("result", {})
            mcp_contents = mcp_result.get("content", [])
            
            # 千帆返回内容为 list 且每个 item 通常是一个字典或包含 text 的片段
            # 其中 text 格式为 details:\nTitle:xxx\nContent:...\nURL:xxx
            for idx, content_item in enumerate(mcp_contents):
                raw_text = content_item.get("text", "")
                if not raw_text:
                    continue
                
                # 尝试精准拆解千帆 text 结构
                title = ""
                item_url = ""
                summary = ""
                
                lines = raw_text.split('\n')
                for line in lines:
                    if line.startswith("Title:"):
                        title = line[len("Title:"):].strip()
                    elif line.startswith("URL:"):
                        item_url = line[len("URL:"):].strip()
                    elif line.startswith("Content:"):
                        summary = line[len("Content:"):].strip()
                
                # 如果没有拆出结构，降级作为整段提取
                if not title:
                    title = f"千帆大模型搜索结果 {idx+1}"
                if not summary:
                    summary = raw_text[:200]
                    
                results.append(SearchResult(
                    title=title,
                    url=item_url,
                    summary=summary[:250],
                    source="千帆大模型搜索MCP",
                ))
            
            if results:
                return results
            else:
                 self.logger.info("千帆 MCP 接口未返回有数据结果，正平滑退避降级至免 Key 爬虫通道...")
                 return self.crawler.search(query, max_results)

        except Exception as e:
            self.logger.warning("⚠️ 百度千帆官方大模型 MCP 接口调用异常 (%s)。安全触发全景降级，切换到免 Key 爬虫...", e)
            # 无缝切换到内置爬虫获取数据并返回 (取长补短)
            return self.crawler.search(query, max_results)



class BaiduSearchProvider(BaseSearchProvider):
    """百度搜索爬虫 - 适合中文新闻搜索（备用方案，无需 API key）"""

    HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        url = f"https://www.baidu.com/s?wd={quote_plus(query)}&ie=utf-8"
        resp = requests.get(url, headers=self.HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")

        results: list[SearchResult] = []
        seen: set[str] = set()

        selectors = ["div.result", "div.c-container", "div.result-op"]
        blocks: list = []
        for sel in selectors:
            blocks = soup.select(sel)
            if blocks:
                break

        for block in blocks[:max_results]:
            title_el = block.select_one("h3 a, h3")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title or len(title) < 4 or title in seen:
                continue
            seen.add(title)

            link_el = title_el if title_el.name == "a" else title_el.select_one("a")
            href = ""
            if link_el and link_el.name == "a":
                href = link_el.get("href", "")
                if href.startswith("/"):
                    href = "https://www.baidu.com" + href

            summary = ""
            # 尝试多种摘要选择器（百度搜索页面结构会变化）
            summary_selectors = [
                ".c-abstract", ".content-right_8Zs40", ".c-span-last", ".abstract",
                ".c-color", ".c-span",  # 新增：百度新版本使用 c-color
                "[class*='abstract']", "[class*='content']"
            ]
            for s in summary_selectors:
                summary_el = block.select_one(s)
                if summary_el:
                    summary = summary_el.get_text(strip=True)[:200]
                    break

            results.append(SearchResult(
                title=title, url=href, summary=summary, source="百度搜索",
            ))

        return results


# ============================================================
# Brave Search 提供商
# ============================================================


class BraveSearchProvider(BaseSearchProvider):
    """Brave Search - 适合国际新闻搜索"""

    # Brave API 付费版免费层单次最多 20，用户预设计划 10
    MAX_RESULTS: int = 10

    BASE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        # 获取代理配置
        self.proxies = self._get_proxy_config()

    def _get_proxy_config(self) -> Optional[dict]:
        """获取代理配置 - Brave Search 需要 SOCKS5 代理（国际搜索）"""
        # 从配置中获取
        proxy_url = self.config.get("proxy") or self.config.get("socks5_proxy")
        if not proxy_url:
            # 从环境变量获取
            import os
            proxy_url = os.environ.get("ALL_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if proxy_url:
            # 支持 socks5:// 和 http:// 格式
            if proxy_url.startswith("socks5://") or proxy_url.startswith("socks5h://"):
                return {"http": proxy_url, "https": proxy_url}
            elif proxy_url.startswith("http://"):
                return {"http": proxy_url, "https": proxy_url}
        return None

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        # 优先级：config > auth.json
        api_key = (
            self.config.get("api_key", "")
            or self.config.get("brave", {}).get("api_key", "")
            or load_api_key_from_auth("brave")
        )
        if not api_key:
            raise RuntimeError("Brave Search API key 未配置（请检查auth.json或配置文件）")

        # Brave 按 count 收费（计划限额内），截断 max_results 不超过 MAX_RESULTS
        _brave_count = min(max_results, self.MAX_RESULTS)
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        }
        params = {"q": query, "count": _brave_count}
        try:
            resp = requests.get(
                self.BASE_URL,
                headers=headers,
                params=params,
                timeout=15,
                proxies=self.proxies if self.proxies else None
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ProxyError as e:
            raise RuntimeError(f"Brave Search 代理连接失败: {e}")
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"Brave Search 连接失败（可能需要代理）: {e}")
        except Exception as e:
            raise RuntimeError(f"Brave Search 请求失败: {e}")

        results: list[SearchResult] = []
        for item in data.get("web", {}).get("results", [])[:max_results]:
            title = item.get("title", "")
            if not title:
                continue
            results.append(SearchResult(
                title=title,
                url=item.get("url", ""),
                summary=item.get("description", ""),
                source="Brave Search",
            ))

        return results


# ============================================================
# Tavily Search 提供商
# ============================================================


class TavilySearchProvider(BaseSearchProvider):
    """Tavily Search - 通用搜索提供商"""

    BASE_URL = "https://api.tavily.com/search"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        # Tavily 不使用代理，不设置 proxies 属性

    def _get_proxy_config(self) -> Optional[dict]:
        """获取代理配置 - Tavily 不需要代理（直连）"""
        # Tavily 直连速度快，不使用代理
        return None

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        # 优先级：config > auth.json
        api_key = (
            self.config.get("api_key", "")
            or self.config.get("tavily", {}).get("api_key", "")
            or load_api_key_from_auth("tavily")
        )
        if not api_key:
            raise RuntimeError("Tavily Search API key 未配置（请检查auth.json或配置文件）")

        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced",
        }
        try:
            resp = requests.post(
                self.BASE_URL,
                json=payload,
                timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"Tavily Search 连接失败: {e}")
        except Exception as e:
            raise RuntimeError(f"Tavily Search 请求失败: {e}")

        results: list[SearchResult] = []
        for item in data.get("results", [])[:max_results]:
            title = item.get("title", "")
            if not title:
                continue
            results.append(SearchResult(
                title=title,
                url=item.get("url", ""),
                summary=item.get("content", ""),
                source="Tavily Search",
            ))

        return results


# ============================================================
# DuckDuckGo Search 提供商
# ============================================================


class DuckDuckGoSearchProvider(BaseSearchProvider):
    """DuckDuckGo Instant Answer API - 免费，无需API key"""

    BASE_URL = "https://api.duckduckgo.com/"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.max_results = config.get("max_results", 25)
        # 从配置或环境变量获取代理设置
        self.proxies = self._get_proxy_config()

    def _get_proxy_config(self) -> Optional[dict]:
        """获取代理配置"""
        # 从配置中获取
        proxy_url = self.config.get("proxy") or self.config.get("socks5_proxy")
        if not proxy_url:
            # 从环境变量获取
            import os
            proxy_url = os.environ.get("ALL_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        
        if proxy_url:
            # 支持 socks5:// 和 http:// 格式
            if proxy_url.startswith("socks5://") or proxy_url.startswith("socks5h://"):
                return {"http": proxy_url, "https": proxy_url}
            elif proxy_url.startswith("http://"):
                return {"http": proxy_url, "https": proxy_url}
        
        return None

    HEADERS: dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        params = {
            "q": query,
            "format": "json",
            "no_redirect": 1,
            "no_html": 1,
        }
        
        try:
            resp = requests.get(
                self.BASE_URL, 
                params=params, 
                headers=self.HEADERS,
                timeout=15,
                proxies=self.proxies if self.proxies else None
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ProxyError as e:
            raise RuntimeError(f"DuckDuckGo 代理连接失败: {e}")
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"DuckDuckGo 连接失败（可能需要代理）: {e}")
        except Exception as e:
            raise RuntimeError(f"DuckDuckGo 请求失败: {e}")

        results: list[SearchResult] = []

        # Abstract (Instant Answer)
        abstract = data.get("Abstract", "")
        if abstract:
            results.append(SearchResult(
                title=data.get("Heading", "DuckDuckGo Instant Answer"),
                url=data.get("AbstractURL", ""),
                summary=abstract[:200],
                source="DuckDuckGo",
            ))

        # RelatedTopics
        for topic in data.get("RelatedTopics", [])[:max_results - len(results)]:
            if isinstance(topic, dict):
                title = topic.get("Text", "") or topic.get("FirstURL", "")
                url = topic.get("FirstURL", "")
                summary = topic.get("Text", "")[:200]
                if title:
                    results.append(SearchResult(
                        title=title,
                        url=url,
                        summary=summary,
                        source="DuckDuckGo",
                    ))

        return results[:max_results]


# ============================================================
# Bing 搜索提供商（中国版 / 国际版）
# ============================================================


class _BaseBingProvider(BaseSearchProvider):
    """Bing 搜索基类 — 国内直连，解析稳定"""

    BASE_URL: str = ""  # 子类覆盖

    HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        url = self.BASE_URL.format(quote_plus(query))
        resp = requests.get(url, headers=self.HEADERS, timeout=15)
        resp.encoding = "utf-8"
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        results: list[SearchResult] = []
        seen: set[str] = set()

        for item in soup.select("li.b_algo")[:max_results * 3]:
            h2 = item.select_one("h2 a")
            if not h2:
                continue
            title = h2.get_text(strip=True)
            href = h2.get("href", "")
            if not title or title in seen or not href:
                continue
            seen.add(title)

            # === 改进：URL 提取逻辑 ===
            # 优先使用 h2 a 的 href（通常是直接的文章链接）
            url_text = href.strip()

            # 如果 h2 href 不可用，尝试从 cite 提取（需要清理）
            if not url_text or not url_text.startswith("http"):
                cite = item.select_one("cite")
                if cite:
                    # 清理 cite 文本：移除多余空格、特殊字符
                    raw_url = cite.get_text(strip=True)
                    # 移除常见分隔符和后续内容
                    for sep in ["›", "|", "-", "::", ">"]:
                        if sep in raw_url:
                            raw_url = raw_url.split(sep)[0].strip()
                    if raw_url.startswith("http"):
                        url_text = raw_url

            if not url_text:
                continue
            # ===========================

            # 摘要: b_lineclamp2 > 任意 p
            snippet_el = item.select_one("p.b_lineclamp2, p")
            summary = snippet_el.get_text(strip=True)[:200] if snippet_el else ""

            # === 新增：URL 过滤逻辑，排除首页/频道页，只保留新闻详情页 ===
            if not self._is_news_article(url_text):
                self.logger.debug("跳过非新闻页面: %s", url_text)
                continue
            # ============================================================

            results.append(
                SearchResult(
                    title=title,
                    url=url_text,
                    summary=summary,
                    source=self.name,
                )
            )
            if len(results) >= max_results:
                break

        return results

    def _is_news_article(self, url: str) -> bool:
        """判断URL是否为新闻文章详情页（排除首页、频道页）"""
        import re
        from urllib.parse import urlparse

        try:
            parsed = urlparse(url)
            path = parsed.path.lower()

            # 1. 排除常见首页/频道页模式
            exclude_patterns = [
                r'^/$',                    # 首页 /
                r'^/index(?:\.(?:html?|s?html?))?$',  # 首页 index.html
                r'^/home/?$',              # 首页 /home
                r'^/channel/?$',           # 频道页 /channel
                r'^/list(?:_|-)?\d+/?$',   # 列表页 /list_123  /list-123
                r'^/tag/?$',               # 标签页 /tag
                r'^/search/?$',            # 搜索页 /search
                r'^/about/?$',             # 关于页 /about
                r'^/contact/?$',           # 联系页 /contact
            ]
            for pattern in exclude_patterns:
                if re.match(pattern, path):
                    return False

            # 2. 检查是否包含新闻常见路径关键词（但不绝对）
            news_patterns = [
                r'/news/', r'/article/', r'/detail/', r'/view/', r'/content/',
                r'/p?[\d]{4,}/',            # 包含4位以上数字（可能是日期或ID）
                r'/\d{8,}/',                # 8位数字（日期格式）
                r'\.s?html?$',              # 静态页面
            ]
            for pattern in news_patterns:
                if re.search(pattern, path):
                    return True

            # 3. 如果路径太短（< 10字符）且没有特殊标记，可能是频道页
            if len(path) < 10 and not any(c.isdigit() for c in path):
                return False

            # 默认：如果无法确定，保守起见排除（避免返回首页）
            return False

        except Exception:
            return False


class BingCNProvider(_BaseBingProvider):
    """Bing 中国版搜索 — 国内直连稳定，中文优先"""

    BASE_URL = "https://cn.bing.com/search?q={}&ensearch=0"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)


class BingIntProvider(_BaseBingProvider):
    """Bing 国际版搜索 — ensearch=1，全球视角"""

    BASE_URL = "https://cn.bing.com/search?q={}&ensearch=1"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)


# ============================================================
# 搜狗搜索提供商
# ============================================================


class SogouProvider(BaseSearchProvider):
    """搜狗网页搜索 — 国内直连，解析结果区块"""

    HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://sogou.com/",
    }

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        url = f"https://sogou.com/web?query={quote_plus(query)}"
        resp = requests.get(url, headers=self.HEADERS, timeout=15)
        resp.encoding = "utf-8"
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        results: list[SearchResult] = []
        seen: set[str] = set()
        # 搜狗结果在 div.results 内的 vrwrap 区块中
        results_div = soup.select_one("div.results")
        if not results_div:
            return results

        for blk in results_div.find_all("div", class_="vrwrap")[: max_results * 2]:
            # 优先：直接 http 链接
            direct = blk.find("a", href=lambda h: h and h.startswith("http"))
            # 备选：js-jump-url 的 data-url
            jsjump = None
            if not direct:
                jsjump = blk.find("a", class_="js-jump-url")

            href = (direct or jsjump or {}).get("href", "") or (
                jsjump.get("data-url", "") if jsjump else ""
            )
            if not href or not href.startswith("http"):
                continue

            # 标题：优先 h3 内的链接文本
            h3_a = blk.find("h3") or blk.find("h2")
            title_el = h3_a.find("a") if h3_a else None
            title = title_el.get_text(strip=True) if title_el else ""
            if not title or title in seen:
                continue
            seen.add(title)

            # 摘要：找最长的文本段落
            best_summary = ""
            for p in blk.find_all(["p", "span", "div"]):
                txt = p.get_text(strip=True)
                if 20 < len(txt) < 300:
                    best_summary = txt
                    break

            results.append(
                SearchResult(
                    title=title,
                    url=href,
                    summary=best_summary[:200],
                    source=self.name,
                )
            )
            if len(results) >= max_results:
                break

        return results


# ============================================================
# 微信搜索提供商（搜狗微信搜索）
# ============================================================


class WechatProvider(BaseSearchProvider):
    """搜狗微信搜索 — 国内直连，抓取微信公众号文章"""

    HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://wx.sogou.com/",
    }

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        url = f"https://wx.sogou.com/weixin?type=2&query={quote_plus(query)}"
        resp = requests.get(url, headers=self.HEADERS, timeout=15)
        resp.encoding = "utf-8"
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        results: list[SearchResult] = []
        seen: set[str] = set()
        for box in soup.select("div.txt-box")[:max_results]:
            h3 = box.find("h3")
            a = h3.find("a", href=True) if h3 else None
            if not a:
                continue
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if not title or title in seen or not href:
                continue
            seen.add(title)

            # 完整 URL（微信搜狗的 href 是相对路径 /link?url=...）
            if href.startswith("/link?"):
                href = "https://wx.sogou.com" + href

            # 摘要
            info_el = box.select_one("p.txt-info")
            summary = info_el.get_text(strip=True)[:200] if info_el else ""

            # 来源公众号
            source_el = box.select_one("span.all-time-y2, span.s1")
            source_name = source_el.get_text(strip=True) if source_el else "微信公众号"

            results.append(
                SearchResult(
                    title=title,
                    url=href,
                    summary=summary,
                    source=f"微信搜狗/{source_name}",
                )
            )

        return results


# ============================================================
# Serper.dev Search 提供商
# ============================================================


class SerperSearchProvider(BaseSearchProvider):
    """Serper.dev (Google) Search - 高质量国际搜索"""

    BASE_URL = "https://google.serper.dev/search"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.api_key = self._get_api_key()

    def _get_api_key(self) -> str:
        """获取 Serper API key"""
        # 优先级：config > auth.json
        api_key = self.config.get("api_key") or load_api_key_from_auth("serper")
        if not api_key:
            raise RuntimeError("Serper API key 未配置（请检查auth.json或配置文件）")
        return api_key

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        payload = json.dumps({
            "q": query,
            "num": min(max_results, 10)  # Serper `num` a参数最多10个
        })
        headers = {
            'X-API-KEY': self.api_key,
            'Content-Type': 'application/json'
        }

        try:
            resp = requests.post(self.BASE_URL, headers=headers, data=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in [401, 403]:
                raise RuntimeError(f"Serper Search failed: Invalid API Key. Please check your `auth.json`.")
            raise RuntimeError(f"Serper HTTP error: {e}")
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"Serper Search 连接失败: {e}")
        except Exception as e:
            raise RuntimeError(f"Serper Search 请求失败: {e}")

        results: list[SearchResult] = []
        for item in data.get("organic", [])[:max_results]:
            title = item.get("title", "")
            if not title:
                continue
            results.append(SearchResult(
                title=title,
                url=item.get("link", ""),
                summary=item.get("snippet", ""),
                source="Serper.dev",
            ))

        return results


# ============================================================
# Mock 搜索提供商（最终故障转移）
# ============================================================


class MockSearchProvider(BaseSearchProvider):
    """模拟数据提供商 - 作为最终 fallback"""

    MOCK_DATA: dict[str, list[dict]] = {
        "politics": [
            {"title": "外交部例行记者会回应热点问题"},
            {"title": "全国人大立法工作计划稳步推进"},
        ],
        "economy": [
            {"title": "A股市场迎来开门红，主要指数全线上涨"},
            {"title": "央行决定下调存款准备金率0.5个百分点"},
        ],
        "astronomy": [
            {"title": "詹姆斯·韦伯望远镜发现新的系外行星大气特征"},
            {"title": "中国空间站科学实验取得重要进展"},
        ],
        "general": [
            {"title": "今日要闻综述：国内外重要新闻一览"},
            {"title": "科技创新推动产业升级取得新突破"},
        ],
    }

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        query_lower = query.lower()
        category = "general"
        if "政治" in query or "politics" in query_lower:
            category = "politics"
        elif "经济" in query or "economy" in query_lower:
            category = "economy"
        elif "天文" in query or "astronomy" in query_lower or "space" in query_lower:
            category = "astronomy"

        mock_items = self.MOCK_DATA.get(category, self.MOCK_DATA["general"])
        return [
            SearchResult(title=item["title"], source=f"模拟数据/{category}")
            for item in mock_items[:max_results]
        ]

# ============================================================
# Jina Search 提供商
# ============================================================
class JinaSearchProvider(BaseSearchProvider):
    """Jina Search - 免费网页搜索 API"""
    BASE_URL = "https://search.jina.ai/search"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.proxies = self._get_proxy_config()

    def _get_proxy_config(self) -> Optional[dict]:
        """获取代理配置 - Jina Search 服务器在海外，可能需要代理"""
        proxy_url = self.config.get("proxy")
        if proxy_url:
            if proxy_url.startswith("socks5://") or proxy_url.startswith("socks5h://"):
                return {"http": proxy_url, "https": proxy_url}
            elif proxy_url.startswith("http://"):
                return {"http": proxy_url, "https": proxy_url}
        return None

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        headers = {"Accept": "application/json"}
        # Jina-Search-Key for paid plans
        api_key = self.config.get("api_key") or load_api_key_from_auth("jina")
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'

        params = {"q": query, "limit": min(max_results, 20)}
        try:
            resp = requests.get(self.BASE_URL, params=params, headers=headers, timeout=20, proxies=self.proxies)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ProxyError as e:
            raise RuntimeError(f"Jina Search proxy connection failed: {e}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 402:
                 raise RuntimeError(f"Jina Search failed: Payment Required. Check your API plan.")
            raise RuntimeError(f"Jina Search HTTP error: {e}")
        except Exception as e:
            raise RuntimeError(f"Jina Search request failed: {e}")
        
        results: list[SearchResult] = []
        for item in data.get("data", [])[:max_results]:
            title = item.get("title", "")
            if not title:
                continue
            results.append(SearchResult(
                title=title,
                url=item.get("url", ""),
                summary=item.get("content", "")[:200],
                source="Jina Search",
            ))
        return results

# ============================================================
# SerpApi Search 提供商
# ============================================================
class SerpApiSearchProvider(BaseSearchProvider):
    """SerpApi Search - Google, Bing, etc. via SerpApi"""
    BASE_URL = "https://serpapi.com/search"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.api_key = self._get_api_key()

    def _get_api_key(self) -> str:
        api_key = self.config.get("api_key") or load_api_key_from_auth("serpapi")
        if not api_key:
            raise RuntimeError("SerpApi API key not configured")
        return api_key

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        params = {
            "q": query,
            "api_key": self.api_key,
            "engine": self.config.get("engine", "google"),
            "num": min(max_results, 100),
        }
        try:
            resp = requests.get(self.BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in [401, 403]:
                raise RuntimeError(f"SerpApi request failed: Invalid API Key. Please check your `auth.json`.")
            raise RuntimeError(f"SerpApi HTTP error: {e}")
        except Exception as e:
            raise RuntimeError(f"SerpApi request failed: {e}")

        results: list[SearchResult] = []
        for item in data.get("organic_results", [])[:max_results]:
            title = item.get("title", "")
            if not title:
                continue
            results.append(SearchResult(
                title=title,
                url=item.get("link", ""),
                summary=item.get("snippet", ""),
                source=f"SerpApi/{params['engine']}",
            ))
        return results


# ============================================================
# 智能搜索路由器
# ============================================================


class SearchRouter:
    """
    智能搜索路由器

    根据新闻类别自动选择最优搜索提供商，实现多提供商自动切换和故障转移。

    类别-提供商映射（按优先级）:
        - politics:   bing_cn -> bing_int -> wechat -> tavily -> mock
        - economy:    bing_cn -> bing_int -> wechat -> tavily -> mock
        - astronomy:  bing_int -> bing_cn -> tavily -> mock
        - general:    bing_cn -> bing_int -> wechat -> tavily -> mock
    """

    DEFAULT_CATEGORY_MAP: dict[str, list[str]] = {
        "politics": ["baidu", "bing_cn", "bing_int", "wechat", "serpapi", "duckduckgo", "tavily", "serper", "mock"],
        "economy": ["baidu", "bing_cn", "bing_int", "wechat", "serpapi", "duckduckgo", "tavily", "serper", "mock"],
        "astronomy": ["serpapi", "bing_int", "brave", "duckduckgo", "tavily", "serper", "baidu", "mock"],
        "general": ["baidu", "serpapi", "bing_cn", "bing_int", "wechat", "duckduckgo", "tavily", "serper", "mock"],
    }

    PROVIDER_CLASSES: dict[str, type[BaseSearchProvider]] = {
        "baidu": BaiduAPISearchProvider,  # 优先使用 API
        "baidu_crawler": BaiduSearchProvider,  # 备用爬虫
        "bing_cn": BingCNProvider,
        "bing_int": BingIntProvider,
        "wechat": WechatProvider,
        "brave": BraveSearchProvider,
        "tavily": TavilySearchProvider,
        "serper": SerperSearchProvider,
        "jina": JinaSearchProvider, # 新增 Jina
        "serpapi": SerpApiSearchProvider, # 新增 SerpApi
        "duckduckgo": DuckDuckGoSearchProvider,
        "mock": MockSearchProvider,
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.logger = logging.getLogger("search_router")
        self.stats = SearchStats()
        self.providers: dict[str, BaseSearchProvider] = {}
        self.category_map: dict[str, list[str]] = {}
        
        # 初始化缓存
        cache_ttl = self.config.get("search", {}).get("cache_ttl", 3600)
        self.cache = SearchCache(ttl_seconds=cache_ttl)

        self._init_providers()
        self._init_category_map()

    def _init_providers(self) -> None:
        search_config = self.config.get("search", {})
        provider_configs = search_config.get("providers", {}) or {}

        # 获取全局代理设置（从 search 配置中）
        global_proxy = search_config.get("proxy")

        # 需要强制走代理的提供商（国际搜索）
        PROXY_REQUIRED_PROVIDERS = {"brave", "duckduckgo"}

        # 必须不走代理的提供商（国内搜索）
        NO_PROXY_PROVIDERS = {"baidu", "baidu_crawler", "bing_cn", "bing_int", "sogou", "wechat"}

        for name, cls in self.PROVIDER_CLASSES.items():
            try:
                # 确保 provider_config 是一个字典
                provider_cfg = provider_configs.get(name, {})
                if provider_cfg is None:
                    provider_cfg = {}
                # 合并全局配置和提供商特定配置
                cfg = {**self.config, **provider_cfg}

                # 代理策略处理
                if name in NO_PROXY_PROVIDERS:
                    # 国内提供商：强制不使用代理，忽略全局代理配置
                    cfg["proxy"] = None
                elif name in PROXY_REQUIRED_PROVIDERS:
                    # 国际提供商：优先使用提供商特定配置，否则使用全局代理
                    if not cfg.get("proxy") and global_proxy:
                        cfg["proxy"] = global_proxy
                    # 如果仍未配置代理，记录警告（但允许运行）
                    if not cfg.get("proxy"):
                        self.logger.warning(
                            "提供商 %s 建议配置代理，当前未设置代理配置", name
                        )
                else:
                    # 其他提供商（如 mock）：使用常规逻辑
                    if not cfg.get("proxy") and global_proxy:
                        cfg["proxy"] = global_proxy

                self.providers[name] = cls(name, cfg)
                self.logger.debug("搜索提供商已注册: %s (proxy=%s)", name, cfg.get("proxy"))
            except Exception as e:
                self.logger.warning("搜索提供商注册失败 %s: %s", name, e)

    def _init_category_map(self) -> None:
        search_config = self.config.get("search", {})
        custom_map = search_config.get("default_providers", {})
        self.category_map = {**self.DEFAULT_CATEGORY_MAP, **custom_map}

    def get_available_providers(self, category: str = "general") -> list[str]:
        order = self.category_map.get(category, self.DEFAULT_CATEGORY_MAP["general"])
        return [name for name in order if name in self.providers]

    def search(
        self,
        query: str,
        category: str = "general",
        max_results: int = 5,
        provider_override: Optional[str] = None,
        use_cache: bool = True,
    ) -> list[SearchResult]:
        """
        执行搜索，自动故障转移

        Args:
            query: 搜索关键词
            category: 新闻类别
            max_results: 最大结果数
            provider_override: 强制使用指定提供商，忽略 category
            use_cache: 是否使用缓存 (默认: True)

        Returns:
            搜索结果列表
        """
        # 检查缓存 (仅在未强制指定提供商时使用缓存)
        if use_cache and not provider_override:
            cached_results = self.cache.get(query, max_results)
            if cached_results is not None:
                # 将字典转换回 SearchResult 对象
                results = [
                    SearchResult(
                        title=r["title"],
                        url=r.get("url", ""),
                        summary=r.get("summary", ""),
                        source=r.get("source", ""),
                    )
                    for r in cached_results
                ]
                self.logger.info(
                    "缓存命中 category=%s query='%s' hits=%d",
                    category, query, len(results),
                )
                return results

        if provider_override:
            if provider_override in self.providers:
                provider_order = [provider_override]
                self.logger.debug("强制使用提供商: %s", provider_override)
            else:
                self.logger.error("强制指定的提供商 '%s' 不存在，将回退到分类路由。", provider_override)
                provider_order = self.get_available_providers(category)
        else:
            provider_order = self.get_available_providers(category)

        if not provider_order:
            self.logger.error("无可用的搜索提供商 (category=%s)", category)
            return []

        last_error: Optional[str] = None
        for provider_name in provider_order:
            provider = self.providers[provider_name]
            start = time.time()
            try:
                # 自动开启 Query 智能语意降级、去噪与英译重塑
                optimized_query = self._auto_translate_and_optimize_query(query, provider_name)
                if optimized_query != query:
                    self.logger.info("意图自适应：提供商 [%s] 的原始检索 '%s' 被重塑为 '%s'", provider_name, query, optimized_query)
                
                results = provider.search(optimized_query, max_results)
                duration = time.time() - start
                ok = bool(results)
                self.stats.record(provider_name, ok, duration)

                if results:
                    # ============================================================
                    # 新增：人类化“全域检索去噪、去重、标题净化”重排引擎
                    # ============================================================
                    refined_results = []
                    seen_urls = set()
                    
                    # 域名黑名单 (剔除垃圾采集站和SEO污染源)
                    BANNED_DOMAINS = {
                        "163.com/dy", "sohu.com/a", "360doc.com", "baidu.com/search",
                        "zhuanlan.zhihu.com/p/ads", "cloud.tencent.com/developer/news"
                    }
                    
                    # 标题高频后缀净化词
                    TITLE_SUFFIX_REMOVALS = [
                        " - 百度百科", "_百度百科", "- 百度知道", "_百度知道", 
                        " - 搜狗搜索", "- 搜狗百科", " - 搜狐", " - 网易", 
                        " - 哔哩哔哩", "_哔哩哔哩", " - CSDN", " - 阿里云开发者社区"
                    ]
                    
                    for r in results:
                        # 1. URL 基础清洗去重
                        clean_url = r.url.strip() if r.url else ""
                        if clean_url:
                            # 剔除常见的无营养跟踪参数
                            if "?" in clean_url and "utm_" in clean_url:
                                clean_url = clean_url.split("?")[0]
                        
                        if clean_url in seen_urls:
                            continue
                        
                        # 2. 阻断被屏蔽域名
                        is_banned = False
                        for banned in BANNED_DOMAINS:
                            if banned in clean_url.lower():
                                is_banned = True
                                break
                        if is_banned:
                            continue
                        
                        # 3. 标题高凝聚力清洗
                        clean_title = r.title.strip()
                        for suffix in TITLE_SUFFIX_REMOVALS:
                            if clean_title.endswith(suffix):
                                clean_title = clean_title[:-len(suffix)].strip()
                            elif suffix in clean_title:
                                clean_title = clean_title.replace(suffix, "").strip()
                        
                        # 4. 去除多余尾部标点
                        clean_title = clean_title.rstrip(" _-|—")
                        
                        r.url = clean_url
                        r.title = clean_title
                        refined_results.append(r)
                        if clean_url:
                            seen_urls.add(clean_url)
                    
                    results = refined_results[:max_results]
                    # ============================================================

                    self.logger.info(
                        "搜索成功 [%s] category=%s query='%s' refined_hits=%d time=%.2fs",
                        provider_name, category, query, len(results), duration,
                    )
                    # 保存到缓存 (仅在未强制指定提供商时)
                    if use_cache and not provider_override:
                        self.cache.set(query, max_results, results)
                    return results

                self.logger.info(
                    "搜索无结果 [%s] category=%s query='%s' time=%.2fs",
                    provider_name, category, query, duration,
                )
            except Exception as e:
                duration = time.time() - start
                self.stats.record(provider_name, False, duration)
                self.stats.record_fallback(provider_name, category, str(e))
                last_error = str(e)
                self.logger.warning(
                    "搜索失败 [%s] category=%s query='%s': %s",
                    provider_name, category, query, e,
                )

        if last_error:
            self.logger.error(
                "所有搜索提供商均失败 (category=%s query='%s'): %s",
                category, query, last_error,
            )
        return []

    def get_stats_summary(self) -> str:
        """获取搜索统计摘要"""
        return self.stats.summary()

    def _auto_translate_and_optimize_query(self, query: str, provider_name: str) -> str:
        """
        海外技术接口智能重构器与高难度多义词消歧引擎：
        1. 若是英文接口且输入含有中文字符，自动提纯与优化。
        2. 识别具有强烈歧义或缩写的商业/技术实体（如火山引擎、豆包、Kimi），
           自动关联补全其品牌背景限定词（如“字节跳动”、“AI”等），防止搜索引擎召回普通生活常识或自然地理误差，彻底消歧。
        3. 在查询过长或失败时，进行自动词频或词性精简重组（从简退避）。
        """
        # --- 多义词消歧逻辑 (防止“火山/字节”搜出“自然火山”或“1个Byte字节大小”) ---
        disambiguation_rules = [
            (r"(火山|火山引擎)(?!.*方舟|.*云)", "火山引擎 火山方舟 大模型生态"),
            (r"(豆包)(?!.*字节|.*ai|.*模型|.*大语言)", "豆包 智能体 大模型"),
            (r"(kimi)(?!.*月之暗面|.*moonshot|.*ai|.*智能体)", "Kimi 智能生成助手 月之暗面"),
            (r"(通义|千问)(?!.*阿里|.*ali|.*qwen)", "通义千问 阿里云 大模型"),
            (r"(元宝)(?!.*腾讯|.*tencent|.*ai|.*hunyuan)", "腾讯 元宝 混元大模型")
        ]
        
        rebuilt_query = query
        import re
        for pattern, replacement in disambiguation_rules:
            # 忽略大小写
            if re.search(pattern, rebuilt_query, re.IGNORECASE):
                rebuilt_query = re.sub(pattern, replacement, rebuilt_query, flags=re.IGNORECASE)
                self.logger.info("意图消歧：检测到多义核心词，原检索 '%s' 已消歧强匹配为 '%s'", query, rebuilt_query)
                break

        # 判断是否需要走英文优化流程的提供商
        ENGLISH_PROVIDERS = {"brave", "serper", "serpapi", "duckduckgo", "bing_int"}
        if provider_name not in ENGLISH_PROVIDERS:
            return rebuilt_query

        # 检查是否含有中文
        has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', rebuilt_query))
        if not has_chinese:
            # 即使全英文，如果含有一些无意义停用词，也可以剔除
            stopwords = {"please", "find", "search", "where", "how", "to", "what", "is", "a", "an", "the"}
            words = rebuilt_query.strip().split()
            if len(words) > 8:
                filtered = [w for w in words if w.lower() not in stopwords]
                if filtered:
                    return " ".join(filtered)
            return rebuilt_query

        # 去噪与粗颗粒翻译备用模型（本地规则做无APIs退避去噪）
        # 提取其中可能存在的英文路径、专有名词、报错码或技术概念
        english_chunks = re.findall(r'[a-zA-Z0-9_\-\.\:\/]+', rebuilt_query)
        
        # 常见的中文疑问词和去噪模板
        stop_patterns = [
            "怎么解决", "如何修复", "为什么出现", "报错怎么弄", "到底什么是",
            "谁能介绍下", "详细的教程", "有什么用", "最新方法"
        ]
        cleared_query = rebuilt_query
        for pat in stop_patterns:
            cleared_query = cleared_query.replace(pat, "")
        
        # 提纯核心概念结构
        cleared_query = cleared_query.strip().rstrip(" ？?_-\"")
        
        if english_chunks:
            # 将中英混杂中的英语专业核心词和精炼后的中文词再次组装
            concept_query = " ".join(english_chunks)
            self.logger.info("意图自适应重合翻译（备用）：中英提纯为: '%s'", concept_query)
            return concept_query
        
        self.logger.info("意图自适应重构：提纯后中文为: '%s'", cleared_query)
        return cleared_query

    def deep_research(
        self,
        query: str,
        category: str = "general",
        max_results: int = 3,
        **kwargs
    ) -> dict[str, Any]:
        """
        【全域级联深度网页提取引擎（Deep Research Engine）】
        
        跑底层常规检索后，取出 Top N 个网页，
        自发发起级联并发爬取，拼装成完整的背景知识案头卷宗！
        """
        # 在最开始，先跑一遍多义词消歧和意图重塑，确保获取最精确的检索条件与科学的缓存 Key
        optimized_query = self._auto_translate_and_optimize_query(query, "bing_cn") # 预先消歧
        self.logger.info("🏆 启动全域级联深度高级搜索 (Deep Research Workflow) ... Query: '%s' -> Optimized: '%s'", query, optimized_query)
        
        # 1. 第一阶段：使用重构后的 Query 快速获取最高相关的多提供商搜索结果
        base_results = self.search(optimized_query, category=category, max_results=max_results, **kwargs)
        if not base_results:
            self.logger.warning("Deep Research: 第一阶段搜索未能获取到任何网页索引。")
            return {
                "success": False,
                "query": query,
                "results_count": 0,
                "dossier": "未获取到任何匹配网页"
            }

        # 2. 第二阶段：多线程异步级联爬取每个网页的全文，并剔除 HTML 并清洗文本
        dossier_entries = []
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        
        import concurrent.futures
        
        def fetch_and_clean_webpage(result: SearchResult) -> dict[str, str]:
            if not result.url or not result.url.startswith("http"):
                return {
                    "title": result.title,
                    "url": result.url,
                    "content": f"无法抓取，因 URL 为空或无效: {result.url}",
                    "source": result.source
                }
            
            # 如果是百度的 link 且未转换，尽最大可能作直接 HEAD 展开
            target_url = result.url
            try:
                if "baidu.com/link?" in target_url:
                    r_head = requests.head(target_url, headers=headers, timeout=5, allow_redirects=True)
                    if r_head.url:
                        target_url = r_head.url
            except Exception:
                pass

            try:
                resp = requests.get(target_url, headers=headers, timeout=12)
                resp.encoding = resp.apparent_encoding or "utf-8"
                if resp.status_code != 200:
                    return {
                        "title": result.title,
                        "url": target_url,
                        "content": f"抓取失败，HTTP 状态码: {resp.status_code}",
                        "source": result.source
                    }
                
                # 清洗正文文本，保留结构
                soup = BeautifulSoup(resp.text, "lxml")
                
                # 剔除非核心文本元素
                for script in soup(["script", "style", "nav", "footer", "iframe", "noscript"]):
                    script.extract()
                
                # 寻找核心文章节点，无则降级到 body
                article_node = soup.select_one("article, main, .article, .post, .content, #content") or soup.body
                raw_text = " ".join(article_node.get_text().split()) if article_node else ""
                
                # 修剪极度过长内容，保留前 6000 各汉字/字符，以节约上下文预算
                clean_text = raw_text[:6000] if len(raw_text) > 6000 else raw_text
                
                return {
                    "title": result.title,
                    "url": target_url,
                    "content": clean_text.strip(),
                    "source": result.source
                }
            except Exception as e:
                return {
                    "title": result.title,
                    "url": target_url,
                    "content": f"抓取抓取过程中引发网络错误异常: {str(e)}",
                    "source": result.source
                }

        self.logger.info("⏳ 正在并发级联下载前 %d 个核心参考源...", len(base_results))
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(fetch_and_clean_webpage, r) for r in base_results]
            for future in concurrent.futures.as_completed(futures):
                try:
                    dossier_entries.append(future.result())
                except Exception as e:
                    self.logger.warning("下载线程抛出意外故障: %s", e)

        # 3. 第三阶段：整合拼接成高级网络知识卷宗 Markdown text
        dossier_markdown = [
            f"# Web Research Dossier\n**Query:** `{query}`\n*Generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}*\n\n"
        ]
        for idx, entry in enumerate(dossier_entries):
            dossier_markdown.append(f"## [{idx+1}] {entry['title']}")
            dossier_markdown.append(f"- **URL:** <{entry['url']}>")
            dossier_markdown.append(f"- **Source:** `{entry['source']}`")
            dossier_markdown.append(f"### [网页详情内容提要 (Cleaned Extract)]\n{entry['content']}\n")
            dossier_markdown.append("-" * 40 + "\n")

        dossier_text = "\n".join(dossier_markdown)
        self.logger.info("✅ Deep Research 卷宗构建成功！总计包含 %d 个参考源, 凝聚正文大小 %.2f KB", len(dossier_entries), len(dossier_text)/1024)
        
        return {
            "success": True,
            "query": query,
            "results_count": len(dossier_entries),
            "dossier": dossier_text,
            "raw_entries": dossier_entries
        }
