"""搜索与页面内容获取：校验、整理和范围读取，不替模型判断研究结论。"""

import hashlib
import ipaddress
import re
from typing import NotRequired, TypedDict
from urllib.parse import urlsplit

import httpx

from .operations import OperationResult, failure

PAGE_CHARS = 24000


class SearchResult(TypedDict):
    """经过校验的单条来源，字段均为非空字符串。"""
    title: str
    url: str
    content: str


class ProcessingInfo(TypedDict):
    """本次结果处理计数，用于说明保留数量与排除原因。"""
    received: int
    kept: int
    removed: dict[str, int]


class SearchResponse(OperationResult):
    """搜索工具的成功或全部过滤结果；请求失败仍沿用 error 返回。"""
    query: str
    results: list[SearchResult]
    processing: ProcessingInfo
    error: NotRequired[str]
    search_parameters: NotRequired[dict]


def normalize_search_results(query: str, response: object) -> SearchResponse:
    """显式校验原始字典并保守整理；TypedDict 本身不执行运行时校验。

    坏条目只记首个失败原因。精确去重限定本次调用，保留首次出现顺序。
    URL 只做语法检查，既不验证可访问性，也不合并域名或查询参数。
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("查询必须为非空字符串")
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise ValueError("Tavily 响应必须包含 results 列表")
    results: list[SearchResult] = []
    removed: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for item in response["results"]:
        reason = ""
        if not isinstance(item, dict):
            reason = "invalid_item"
        elif not isinstance(item.get("url"), str):
            reason = "invalid_url"
        else:
            url = item["url"].strip()
            try:
                parts = urlsplit(url)
                if (parts.scheme not in {"http", "https"} or not parts.hostname
                        or any(c.isspace() or ord(c) < 32 for c in url)):
                    reason = "invalid_url"
                # 读取 port 会检查非法端口；不对合法 URL 做重写。
                _ = parts.port
            except ValueError:
                reason = "invalid_url"
            if not reason:
                content = item.get("content")
                title = item.get("title")
                if not isinstance(content, str) or (title is not None and not isinstance(title, str)):
                    reason = "invalid_fields"
                else:
                    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
                    title = (title or "").replace("\r\n", "\n").replace("\r", "\n").strip() or url
                    if not content:
                        reason = "empty_content"
                    elif (url, content) in seen:
                        reason = "duplicate"
                    else:
                        seen.add((url, content))
                        results.append({"title": title, "url": url, "content": content})
        if reason:
            removed[reason] = removed.get(reason, 0) + 1
    output: SearchResponse = {
        "operation": "tavily_search", "status": "success" if results else "empty",
        "content_kind": "snippet", "query": query, "results": results,
        "processing": {"received": len(response["results"]), "kept": len(results), "removed": removed},
    }
    if response["results"] and not results:
        output["status"] = "failed"
        output["error_code"] = "all_filtered"
        output["error"] = "Tavily 返回的条目全部被过滤，本次没有可用搜索证据。"
    return output


def request_tavily(endpoint: str, body: dict, *, client: httpx.Client, api_key: str) -> dict:
    """发送一次 Tavily 请求，网络异常由操作入口转换为结果。"""
    response = client.post(f"https://api.tavily.com/{endpoint}",
                           headers={"Authorization": f"Bearer {api_key}"}, json=body, timeout=40)
    response.raise_for_status()
    return response.json()


def retrieval_error(operation: str, exc: Exception) -> dict:
    """保留错误类别和状态码，不输出认证信息或原始服务端异常正文。"""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return failure(operation, "http_error", f"Tavily 请求失败（HTTP {code}）。", http_status=code)
    if isinstance(exc, httpx.TimeoutException):
        return failure(operation, "timeout", "Tavily 请求超时，未取得本次内容。")
    if isinstance(exc, httpx.RequestError):
        return failure(operation, "network_error", "Tavily 网络连接失败。")
    return failure(operation, "invalid_response", "Tavily 返回的数据格式异常。")


def tavily_search(query: str, *, client: httpx.Client, api_key: str,
                  max_results: int = 3, include_domains: list[str] | None = None) -> dict:
    """返回搜索片段和处理数量；不隐式读取每个网页或改变来源排序。"""
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query 必须为非空字符串')
    if type(max_results) is not int or not 1 <= max_results <= 10:
        raise ValueError('max_results 必须为 1–10 的整数')
    domains = []
    if include_domains is not None:
        if not isinstance(include_domains, list) or len(include_domains) > 10:
            raise ValueError('include_domains 必须为最多 10 个域名的列表')
        for value in include_domains:
            if not isinstance(value, str):
                raise ValueError('include_domains 中每项必须为域名字符串')
            domain = value.strip().lower()
            labels = domain.split('.')
            if (len(domain) > 253 or len(labels) < 2
                    or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)
                    or domain.endswith(('.localhost', '.local')) or labels[-1].isdigit()):
                raise ValueError('include_domains 只接受公开域名，不接受 URL、路径、IP 或通配符')
            if domain not in domains:
                domains.append(domain)
    parameters = {'search_depth': 'basic', 'max_results': max_results,
                  'include_answer': False, 'include_raw_content': False}
    if domains:
        parameters.update(include_domains=domains, include_domains_mode='filter')
    try:
        raw = request_tavily('search', {'query': query, **parameters}, client=client, api_key=api_key)
        result = normalize_search_results(query, raw)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        result = retrieval_error('tavily_search', exc)
        result['query'] = query
    result['search_parameters'] = parameters
    return result


def valid_page_url(url: str) -> bool:
    """只接受无凭据的 HTTP(S) 公网形式地址，不把代理 DNS 地址误当页面地址。"""
    try:
        p = urlsplit(url)
        if (p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password
                or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url):
            return False
        _ = p.port
        host = p.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local")) or "." not in host:
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def read_page(url: str, *, client: httpx.Client, api_key: str, pages: dict, start: int = 0) -> dict:
    """提取指定 URL；start=0 重新获取，后续范围复用当前会话中同一内容快照。

    provider 不提供最终地址或完整性保证，故不把报告 URL 称为最终地址。
    partial 只说明本次返回快照的一个范围；全部范围也不证明网站完整抓取。
    """
    if not isinstance(url, str) or not valid_page_url(url) or type(start) is not int or start < 0:
        return failure("read_page", "invalid_arguments", "需要合法公开 HTTP(S) URL 和非负整数 start。")
    if start == 0:
        # 新读取失败时不留旧快照供同一输入继续误读；整个输入失败仍由 Session 回退。
        pages.pop(url, None)
        try:
            raw = request_tavily("extract", {"urls": [url], "extract_depth": "basic", "format": "markdown",
                                 "include_images": False, "timeout": 20}, client=client, api_key=api_key)
            if not isinstance(raw, dict) or not isinstance(raw.get("results"), list) or not isinstance(raw.get("failed_results"), list):
                raise ValueError("结果结构异常")
            if not raw["results"]:
                if raw["failed_results"]:
                    failed = raw["failed_results"][0]
                    if not isinstance(failed, dict) or not isinstance(failed.get("error"), str):
                        raise ValueError("失败项结构异常")
                    # 仅解释明确的服务端原因，不把认证头或任意错误正文回传。
                    detail = failed["error"].lower()
                    reasons = [("404", "not_found", "Tavily 报告目标页面返回 404。"),
                               ("403", "forbidden", "Tavily 报告目标页面拒绝访问（403）。"),
                               ("timeout", "timeout", "Tavily 报告提取页面超时。"),
                               ("timed out", "timeout", "Tavily 报告提取页面超时。")]
                    reason_code, message = next(((code, text) for marker, code, text in reasons if marker in detail),
                                                ("unspecified", "Tavily 未能提取指定页面，未提供可识别的原因。"))
                    return failure("read_page", "extraction_failed", message, requested_url=url, reason_code=reason_code)
                return failure("read_page", "empty_content", "未取得指定页面正文。", requested_url=url)
            if len(raw["results"]) != 1:
                raise ValueError("单 URL 提取返回了多个条目")
            item = raw["results"][0]
            content = item["raw_content"]
            reported_url = item["url"]
            if not isinstance(content, str) or not isinstance(reported_url, str) or not valid_page_url(reported_url):
                raise ValueError("字段类型异常")
            content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not content:
                return failure("read_page", "empty_content", "提取结果没有可读正文。", requested_url=url)
            title = item.get("title")
            pages[url] = {"title": title.strip() if isinstance(title, str) and title.strip() else reported_url,
                          "url": reported_url, "content": content,
                          "snapshot_id": hashlib.sha256(content.encode()).hexdigest()[:16]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return {**retrieval_error("read_page", exc), "requested_url": url}
    if url not in pages:
        return failure("read_page", "missing_snapshot", "没有可继续读取的快照，请先从 start=0 读取。", requested_url=url)
    page = pages[url]
    total = len(page["content"])
    if start >= total:
        return failure("read_page", "invalid_range", "start 超出已取得文本范围。", total_characters=total)
    end = min(start + PAGE_CHARS, total)
    return {"operation": "read_page", "status": "success" if start == 0 and end == total else "partial",
            "content_kind": "extracted_text", "requested_url": url, "url": page["url"], "final_url": None,
            "title": page["title"], "content": page["content"][start:end], "snapshot_id": page["snapshot_id"],
            "start": start, "end": end, "total_characters": total, "next_start": end if end < total else None,
            "coverage": "范围仅针对本次提取文本，不保证覆盖网页全部内容。"}
