"""分配最新工具批次的请求预算，统计实际交付的正文范围。"""
import json


def _size(value):
    return len(json.dumps(value, ensure_ascii=False))


def latest_batch_indices(messages):
    """只接受最新 assistant 的完整结果组；缺失或重复的结果不重排。"""
    anchor = next((i for i in range(len(messages)-1, -1, -1)
                   if messages[i].get('role') == 'assistant'), None)
    if anchor is None:
        return []
    calls = messages[anchor].get('tool_calls', [])
    ids = [call.get('id') for call in calls if isinstance(call, dict)]
    tail = list(range(anchor+1, len(messages)))
    actual = [messages[i].get('tool_call_id') for i in tail]
    if (not ids or len(set(ids)) != len(ids) or len(actual) != len(ids)
            or set(actual) != set(ids) or any(messages[i].get('role') != 'tool' for i in tail)):
        return []
    return tail


def fit_latest_batch(messages, documents, budget_chars, *, payload_transform=None):
    """返回 {消息位置: 替换消息} 与度量；预算是这些工具消息 JSON 大小之和。

    调用者负责完整请求预算以及提交实际投影。cannot_fit 不发布半成品替换；
    旧消息、最新进展、工具定义和用户问题都不在此函数的修改范围。
    """
    if type(budget_chars) is not int:
        raise ValueError('批次预算需要整数字符数')
    indices = latest_batch_indices(messages)
    before = sum(_size(messages[i]) for i in indices)
    info = {'status': 'unchanged', 'characters_before': before, 'characters_after': before,
            'budget_characters': budget_chars, 'tool_results': len(indices), 'policy': 'equal_body_cap_v1'}
    if not indices or before <= budget_chars:
        return {}, info
    anchor = indices[0]-1
    calls = {call['id']: call for call in messages[anchor]['tool_calls']}
    views = {}
    for index in indices:
        try:
            payload = json.loads(messages[index]['content'])
            arguments = json.loads(calls[messages[index]['tool_call_id']]['function']['arguments'])
            if not isinstance(arguments, dict):
                continue
        except (ValueError, KeyError, TypeError):
            continue
        view = documents.delivery_view(payload, arguments)
        if view is not None:
            views[index] = view

    def render(share):
        replacements = {i: dict(messages[i], content=json.dumps(
                            payload_transform(view.render(share)) if payload_transform else view.render(share),ensure_ascii=False))
                        for i, view in views.items()}
        size = sum(_size(replacements.get(i, messages[i])) for i in indices)
        return replacements, size

    minimum, floor = render(0)
    while floor > budget_chars:
        shrinkable = sorted(views, key=lambda i: _size(minimum[i]), reverse=True)
        if not any(views[i].trim_navigation() for i in shrinkable):
            info.update(status='cannot_fit', minimum_characters=floor)
            return {}, info
        minimum, floor = render(0)
    low, high = 0, max((len(messages[i].get('content', '')) for i in indices), default=0)
    best, after = minimum, floor
    while low <= high:
        share = (low+high)//2
        candidate, size = render(share)
        if size <= budget_chars:
            best, after, low = candidate, size, share+1
        else:
            high = share-1
    best = {i: value for i, value in best.items() if value != messages[i]}
    info.update(status='reflowed', characters_after=after, minimum_characters=floor,
                body_cap_characters=high,
                not_displayed_rows=sum(view.omitted_rows for view in views.values()),
                not_displayed_outline_rows=sum(view.omitted_outline_rows for view in views.values()))
    return best, info


def quote_projection(progress):
    """恢复资料中已校验原句的交付投影；正文只在实际请求中展示一次。"""
    if not isinstance(progress,dict) or progress.get('schema_version')!=2:
        return None
    findings=[{'evidence_quotes':finding['evidence_quotes']}
              for finding in progress.get('state',{}).get('findings',[])
              if isinstance(finding,dict) and finding.get('evidence_quotes')]
    return {'operation':'update_research','schema_version':2,'content_kind':'source_quotes',
            'state':{'findings':findings}} if findings else None


def remember_delivery(delivered, messages):
    """合并实际请求中的正文范围；目录和被外置的正文不计为已交付。"""
    for message in messages:
        if message.get('role') != 'tool':
            continue
        try:
            payload = json.loads(message['content'])
        except (ValueError, TypeError, KeyError):
            continue
        if not isinstance(payload, dict) or payload.get('offloaded'):
            continue
        operation = payload.get('operation')
        if operation in {'read_page', 'read_document', 'read_section'}:
            rows = [payload]
        elif operation == 'tavily_search':
            rows = payload.get('results', [])
        elif operation == 'find_in_documents':
            rows = payload.get('matches', [])
        elif operation=='update_research' and payload.get('schema_version')==2:
            state=payload.get('state',{})
            rows=[{'reference':q.get('reference'),'content':q.get('text')}
                  for finding in state.get('findings',[]) if isinstance(finding,dict)
                  for q in finding.get('evidence_quotes',[]) if isinstance(q,dict)]
        elif operation=='submit_answer' and payload.get('content_kind')=='structured_answer' and payload.get('schema_version')==1:
            rows=[{'reference':q.get('reference'),'content':q.get('text')}
                  for q in payload.get('source_quotes',[]) if isinstance(q,dict)]
        else:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            ref = row.get('reference')
            if not isinstance(ref, dict) or not isinstance(row.get('content'), str):
                continue
            start, end, doc_id = ref.get('start'), ref.get('end'), ref.get('document_id')
            if (not isinstance(doc_id, str) or type(start) is not int or type(end) is not int
                    or start < 0 or end <= start or end-start != len(row['content'])):
                continue
            spans = sorted([*delivered.get(doc_id, []), (start, end)])
            merged = []
            for left, right in spans:
                if merged and left <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], right))
                else:
                    merged.append((left, right))
            delivered[doc_id] = merged
