"""Search validation around the asynchronous Tavily integration."""
import ipaddress
from urllib.parse import urlsplit
from langchain_tavily import TavilySearch, TavilyExtract
from langchain_core.tools import ToolException


def valid_page_url(url):
    try:
        p = urlsplit(url)
        if (p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password
                or any(c.isspace() or ord(c) < 32 for c in url) or '\\' in url):
            return False
        _ = p.port
        host = p.hostname.rstrip('.').lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local')) or '.' not in host:
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


class Retrieval:
    def __init__(self, api_key):
        self.search_tool = TavilySearch(tavily_api_key=api_key, max_results=5,
                                       include_answer=False, include_raw_content=False)
        self.extract_tool = TavilyExtract(tavily_api_key=api_key, format='markdown')

    async def search(self, query, *, search_depth='basic', include_domains=None, exclude_domains=None):
        return await self._call(self.search_tool, dict(query=query, search_depth=search_depth,
                                include_domains=include_domains or [], exclude_domains=exclude_domains or []))

    async def fetch(self, url):
        if not valid_page_url(url):
            return {'error': 'invalid_url', 'message': '需要无凭据的公网 HTTP(S) URL'}
        return await self._call(self.extract_tool, {'urls': [url]})

    @staticmethod
    async def _call(tool, arguments):
        try:
            result = await tool.ainvoke(arguments)
        except ToolException:
            return {'error': 'retrieval_failed', 'message': '服务未返回可用资料；不代表资料不存在。'}
        if not isinstance(result, dict) or result.get('error'):
            error = result.get('error') if isinstance(result, dict) else result
            return {'error': 'retrieval_failed', 'detail_type': type(error).__name__,
                    'message': '获取未成功，尚不能判断页面内容。'}
        return result
