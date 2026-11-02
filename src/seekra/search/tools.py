"""Product tools publish sources; SDK tools handle file access."""
import asyncio
from typing import Literal
from langchain_core.tools import tool
from seekra.search.retrieval import valid_page_url


def search_tools(retrieval, sources):
    @tool
    async def search_web(query: str, search_depth: Literal['basic', 'advanced'] = 'basic',
                         include_domains: list[str] | None = None,
                         exclude_domains: list[str] | None = None) -> dict:
        """Search web snippets, not full pages. Fetch important sources before relying on their conditions."""
        result = await retrieval.search(query, search_depth=search_depth,
                                        include_domains=include_domains, exclude_domains=exclude_domains)
        if result.get('error'):
            return result
        items = []
        for item in result.get('results', []):
            url, content = item.get('url', ''), item.get('content', '')
            if not valid_page_url(url) or not isinstance(content, str) or not content.strip():
                continue
            ref = await asyncio.to_thread(sources.publish, url=url, content=content,
                                          kind='snippet', title=item.get('title', ''))
            items.append({**ref, 'snippet': content})
        return {'query': query, 'sources': items, 'status': 'ok' if items else 'no_results'}

    @tool
    async def fetch_page(url: str) -> dict:
        """Fetch an immutable page snapshot. Read the returned path with read_file before citing conditions."""
        result = await retrieval.fetch(url)
        if result.get('error'):
            return result
        items = []
        for item in result.get('results', []):
            content, actual = item.get('raw_content', ''), item.get('url', url)
            if not valid_page_url(actual) or not isinstance(content, str) or not content.strip():
                continue
            items.append(await asyncio.to_thread(sources.publish, url=actual, requested_url=url,
                         content=content, kind='page', title=item.get('title', '')))
        return {'status': 'ok' if items else 'no_content', 'sources': items,
                'instruction': '用 read_file / grep 回查快照；提取结果不保证包含页面的全部内容。'}
    return [search_web, fetch_page]
