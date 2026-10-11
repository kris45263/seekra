"""会话资料的稳定引用、Markdown 小节及本地阅读视图。"""
from base64 import urlsafe_b64decode, urlsafe_b64encode
from copy import copy, deepcopy
from dataclasses import dataclass
import hashlib
import json
import re


READ_CHARS = 6000
INTRO_CHARS = 1800
SNIPPET_CHARS = 1200
INDEX_SIZE = 10
OUTLINE_SIZE = 20
DOCUMENT_OPERATIONS = {'tavily_search', 'read_page', 'list_documents',
                       'find_in_documents', 'read_document', 'read_section'}
_PROTOCOL_FIELDS = {'operation', 'status', 'content_kind', 'error_code', 'reason_code',
                    'source_id', 'document_id', 'section_ref', 'match_ref', 'snapshot_id',
                    'run_id', 'run_status', 'cursor', 'next_cursor', 'previous_cursor',
                    'outline_cursor', 'tool', 'view'}


def canonical(value, secrets=(), key=''):
    """只在文本值上遮蔽，保持协议字段和 JSON 结构可读。"""
    if isinstance(value, str):
        if key in _PROTOCOL_FIELDS:
            return value
        value = value.replace('\r\n', '\n').replace('\r', '\n')
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            value = value.replace(secret, '[已隐藏密钥]')
        return value
    if isinstance(value, dict):
        return {k: canonical(v, secrets, k) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical(v, secrets) for v in value]
    return value


def sections_v1(text, document_id):
    """识别 ATX/Setext 标题；范围含子节，排除围栏和缩进代码。"""
    headings, fence, previous = [], None, None
    position = 0
    for line in text.splitlines(keepends=True):
        plain = line.rstrip('\n')
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', plain)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
            previous = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
            previous = None
        else:
            atx = re.match(r'^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$', plain)
            underline = re.match(r'^ {0,3}(=+|-+)[ \t]*$', plain)
            if atx:
                title = re.sub(r'[ \t]+#+[ \t]*$', '', atx[2] or '').strip()
                headings.append((len(atx[1]), title, position))
                previous = None
            elif underline and previous:
                headings.append((1 if underline[1][0] == '=' else 2, previous[1], previous[0]))
                previous = None
            elif plain.strip() and not re.match(r'^(?: {4}|\t| {0,3}(?:>|[-+*] |\d+[.)] |\|))', plain):
                previous = (position, plain.strip())
            else:
                previous = None
        position += len(line)
    result, stack = [], []
    for level, title, start in headings:
        while stack and result[stack[-1]]['level'] >= level:
            result[stack.pop()]['end'] = start
        path = [result[i]['title'] for i in stack] + [title]
        result.append({'section_ref': f'{document_id}-H{len(result)+1}', 'title': title,
                       'path': path, 'level': level, 'start': start, 'end': len(text)})
        stack.append(len(result)-1)
    return tuple(result)


@dataclass(frozen=True)
class Document:
    id: str
    source_id: str
    kind: str
    title: str
    requested_url: str
    url: str
    content: str
    snapshot_id: str
    record_id: str
    run_id: str | None
    time: str | None
    query: str
    sections: tuple


class Documents:
    """记录是事实来源；这个索引可以从记录重建，不另外写文件。"""
    def __init__(self):
        self._records = None
        self._offset = 0
        self._scope = ''
        self._secrets = ()
        self._epoch = '0'
        self._source_number = 0
        self._sources = {}
        self._counts = {}
        self._docs = {}
        self._by_record = {}
        self._pages = {}
        self._runs = {}

    def sync(self, records, scope, secrets=()):
        if self._records is not records or self._offset > len(records):
            self.__init__()
            self._records = records
        self._scope = scope
        self._secrets = secrets
        for record in records[self._offset:]:
            kind = record['kind']
            if kind == 'session_reset':
                self._sources, self._counts, self._docs = {}, {}, {}
                self._by_record, self._pages, self._runs = {}, {}, {}
                self._epoch = record['id']
            elif kind == 'run_started':
                self._runs[record['run_id']] = (record.get('question', ''), 'running')
            elif kind in {'run_ended', 'run_resumed'}:
                run_id = record['run_id']
                question = self._runs.get(run_id, ('', ''))[0]
                self._runs[run_id] = (question, record.get('status', 'running'))
            elif kind == 'tool_completed':
                self._ingest(record)
        self._offset = len(records)
        return self

    def _fork(self):
        candidate = copy(self)
        for field in ('_sources', '_counts', '_docs', '_by_record', '_pages', '_runs'):
            setattr(candidate, field, dict(getattr(self, field)))
        return candidate

    @staticmethod
    def _payload(record):
        if 'retrieval_result' in record:
            return record['retrieval_result']
        try:
            return json.loads(record['result'])
        except (ValueError, TypeError, KeyError):
            return None

    def _add(self, record, kind, title, requested_url, url, content, snapshot='', query=''):
        if not all(isinstance(v, str) for v in (requested_url, url, content)) or not content or not requested_url:
            return None
        if requested_url not in self._sources:
            self._source_number += 1
            self._sources[requested_url] = f'S{self._source_number}'
        source = self._sources[requested_url]
        letter = {'search_snippet': 'R', 'page_text': 'P', 'extracted_fragment': 'F'}[kind]
        key = source, letter
        number = self._counts.get(key, 0) + 1
        self._counts[key] = number
        doc_id = f'{source}-{letter}{number}'
        doc = Document(doc_id, source, kind, title if isinstance(title, str) else url,
                       requested_url, url, content, snapshot, record['id'], record.get('run_id'),
                       record.get('time'), query, sections_v1(content, doc_id) if kind == 'page_text' else ())
        self._docs[doc_id] = doc
        return doc_id

    def _ingest(self, record):
        if record.get('documents_version', 1) != 1:
            raise ValueError('不支持的资料视图版本')
        payload = self._payload(record)
        if not isinstance(payload, dict) or payload.get('status') not in {'success', 'partial'}:
            return
        ids = []
        if payload.get('operation') == 'tavily_search':
            for row in payload.get('results', []):
                if not isinstance(row, dict):
                    continue
                ref = self._add(record, 'search_snippet', row.get('title'), row.get('url'),
                                row.get('url'), row.get('content'), query=payload.get('query', ''))
                if ref:
                    ids.append(ref)
        elif payload.get('operation') == 'read_page':
            url = payload.get('requested_url') or payload.get('url')
            snapshot = payload.get('snapshot_id', '')
            page = record.get('page_updates', {}).get(url)
            existing = self._pages.get((url, snapshot))
            if existing and payload.get('start', 0) != 0:
                ids = [existing]
            else:
                if isinstance(page, dict) and isinstance(page.get('content'), str):
                    content, kind = page['content'], 'page_text'
                elif existing:
                    content, kind = self._docs[existing].content, 'page_text'
                else:
                    content = payload.get('content')
                    kind = ('page_text' if payload.get('start') == 0 and isinstance(content, str)
                            and payload.get('total_characters') == len(content) else 'extracted_fragment')
                ref = self._add(record, kind, payload.get('title'), url, payload.get('url', url), content, snapshot)
                if ref:
                    ids = [ref]
                    if kind == 'page_text':
                        self._pages[(url, snapshot)] = ref
        if ids:
            self._by_record[record['id']] = tuple(ids)

    def prepare(self, record, secrets):
        """提交前在副本生成视图；写入失败不会发布新资料或消耗引用编号。"""
        payload = self._payload(record)
        if not isinstance(payload, dict) or payload.get('operation') not in DOCUMENT_OPERATIONS:
            return record
        record = deepcopy(record)
        payload = canonical(payload, secrets)
        updates = {canonical(url, secrets): canonical(page, secrets)
                   for url, page in record.get('page_updates', {}).items()}
        for page in updates.values():
            if isinstance(page, dict) and isinstance(page.get('content'), str):
                page['snapshot_id'] = hashlib.sha256(page['content'].encode()).hexdigest()[:16]
        if payload.get('operation') == 'read_page':
            page = updates.get(payload.get('requested_url'))
            if page:
                payload['snapshot_id'] = page['snapshot_id']
                payload['total_characters'] = len(page['content'])
                start = payload.get('start', 0)
                payload['content'] = page['content'][start:start+24000]
                payload['end'] = start + len(payload['content'])
                payload['next_start'] = payload['end'] if payload['end'] < len(page['content']) else None
        record['page_updates'] = updates
        record['documents_version'] = 1
        record['result'] = json.dumps(payload, ensure_ascii=False)
        if payload.get('operation') in {'read_page', 'tavily_search'}:
            record['retrieval_result'] = payload
            candidate = self._fork()
            candidate._ingest(record)
            view = candidate._present(record, payload)
            record['result'] = json.dumps(view, ensure_ascii=False)
        return record

    def _summary(self, doc):
        question, status = self._runs.get(doc.run_id, ('', 'unknown'))
        return {'source_id': doc.source_id, 'document_id': doc.id, 'title': doc.title,
                'url': doc.url, 'requested_url': doc.requested_url, 'content_kind': doc.kind,
                'total_characters': len(doc.content), 'record_id': doc.record_id,
                'run_status': status, 'question': canonical(question, self._secrets)[:200],
                'retrieved_date': doc.time[:10] if doc.time else None, 'retrieved_at': doc.time,
                'snapshot_id': doc.snapshot_id, 'query': doc.query}

    def _present(self, record, payload):
        ids = self._by_record.get(record['id'], ())
        if not ids:
            return payload
        if payload['operation'] == 'tavily_search':
            rows = []
            for ref in ids:
                doc = self._docs[ref]
                duplicate = next((old for old in self._docs.values()
                                  if int(old.record_id) < int(doc.record_id) and old.kind == doc.kind
                                  and old.source_id == doc.source_id and old.content == doc.content), None)
                end = min(200 if duplicate else SNIPPET_CHARS, len(doc.content))
                rows.append({**self._summary(doc), 'content': doc.content[:end],
                             'start': 0, 'end': end, 'truncated': end < len(doc.content),
                             'reference': self._reference(doc, 0, end),
                             'evidence_ref': f'{doc.id}@0-{end}',
                             'read_tool': 'read_document',
                             **({'duplicate_of': duplicate.id,
                                 'notice': '同一来源的完整搜索片段与此前资料相同；本次保留新查询及短预览，可本地展开完整片段。'}
                                if duplicate else {})})
            return {'operation': 'tavily_search', 'status': payload['status'],
                    'content_kind': 'snippet', 'query': payload.get('query'), 'results': rows,
                    'notice': '这是搜索片段；truncated 只表示保存的片段是否在本次展示中截短，不表示整页完整。可用 read_document 回查片段，用 read_page(url) 获取页面。'}
        doc = self._docs[ids[0]]
        start = payload.get('start', 0) if doc.kind == 'page_text' else 0
        view = self._read(doc, start, len(doc.content), INTRO_CHARS, 'document', doc.id, 0)
        outline = self._outline(doc, 0)
        return {**view, 'operation': 'read_page', 'snapshot_id': doc.snapshot_id,
                'final_url': payload.get('final_url'), 'outline': outline['outline'],
                'outline_cursor': outline['next_cursor'], 'outline_available': bool(doc.sections),
                'outline_continuation': outline['continuation'],
                'coverage': '范围仅针对这份提取文本；目录来自其实际标题，不保证网站完整。'}

    def _document(self, ref):
        if not isinstance(ref, str) or ref not in self._docs:
            if isinstance(ref, str) and '-H' in ref:
                doc = self._docs.get(ref.rsplit('-H', 1)[0])
                if doc and any(section['section_ref'] == ref for section in doc.sections):
                    raise ValueError(f'{ref} 是小节引用，请用 read_section(section_ref="{ref}") 读取；'
                                     '无需把小节位置改写成正文引用')
            raise ValueError('没有可访问的资料，请先用 list_documents 查看当前会话')
        return self._docs[ref]

    @staticmethod
    def _reference(doc, start, end):
        return {'source_id': doc.source_id, 'document_id': doc.id, 'start': start, 'end': end}

    def validate_reference(self, reference):
        """校验对保存文本的引用；不判断文字是否支持模型的结论。"""
        if isinstance(reference, str):
            match = re.fullmatch(r'([^@]+)@(\d+)-(\d+)', reference)
            if match is None:
                if '-H' in reference:
                    self._document(reference)
                raise ValueError('需要实际正文结果的 evidence_ref 或完整 reference 对象')
            doc = self._document(match[1])
            reference = self._reference(doc, int(match[2]), int(match[3]))
        if not isinstance(reference, dict) or set(reference) != {'source_id', 'document_id', 'start', 'end'}:
            raise ValueError('正文引用需要 source_id、document_id、start、end')
        doc = self._document(reference['document_id'])
        if reference['source_id'] != doc.source_id:
            raise ValueError('来源与资料引用不一致')
        try:
            self._number(reference['start'], 0, len(doc.content)-1)
            self._number(reference['end'], reference['start']+1, len(doc.content))
        except (ValueError, TypeError) as exc:
            raise ValueError(f'{doc.id} 读取范围不合法：需要 0 <= start < end <= {len(doc.content)}。'
                             '请复制实际返回的 evidence_ref；快照范围内也不代表正文已经交付') from exc
        return dict(reference)

    def _cursor(self, kind, **fields):
        value = {'v': 1, 'scope': self._scope, 'epoch': self._epoch, 'kind': kind, **fields}
        return urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode().rstrip('=')

    def _decode(self, cursor, kind):
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise ValueError('读取游标不合法')
        try:
            value = json.loads(urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        except (ValueError, UnicodeError):
            raise ValueError('读取游标不合法') from None
        if (not isinstance(value, dict) or value.get('v') != 1 or value.get('scope') != self._scope
                or value.get('epoch') != self._epoch or value.get('kind') != kind):
            raise ValueError('游标不属于当前会话或读取操作')
        return value

    @staticmethod
    def _number(value, low, high):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('读取范围不合法')
        return value

    def _outline(self, doc, offset):
        self._number(offset, 0, len(doc.sections))
        rows = [dict(section, path=list(section['path']),
                     characters=section['end']-section['start'],
                     preview=doc.content[section['start']:min(section['start']+180, section['end'])],
                     preview_start=section['start'], preview_end=min(section['start']+180, section['end']))
                for section in doc.sections[offset:offset+OUTLINE_SIZE]]
        end = offset + len(rows)
        cursor = self._cursor('outline', ref=doc.id, offset=end) if end < len(doc.sections) else None
        return {**self._summary(doc), 'operation': 'read_document', 'status': 'success',
                'outline': rows, 'next_cursor': cursor, 'outline_available': bool(doc.sections),
                'continuation': {'tool': 'read_document', 'arguments': {
                    'document_id': doc.id, 'view': 'outline', 'cursor': cursor}} if cursor else None}

    def _read(self, doc, start, stop, size, kind, ref, lower):
        self._number(start, lower, stop)
        if start >= stop:
            raise ValueError('起点超出目标正文范围')
        end = min(start + size, stop)
        if end < stop:
            boundary = doc.content.rfind('\n\n', start + size//2, end)
            if boundary >= 0:
                end = boundary + 2
        more = end < stop
        cursor = self._cursor(kind, ref=ref, start=end) if more else None
        tool = 'read_section' if kind == 'section' else 'read_document'
        key = 'section_ref' if kind == 'section' else 'document_id'
        return {**self._summary(doc), 'operation': 'read_section' if kind == 'section' else 'read_document',
                'status': 'success' if start == lower and end == stop else 'partial',
                'content': doc.content[start:end], 'start': start, 'end': end,
                'target_start': lower, 'target_end': stop, 'complete': start == lower and end == stop,
                'remaining_characters': stop-end, 'reached_end': not more,
                'next_cursor': cursor, 'next_start': end if more else None,
                'continuation': {'tool': tool, 'arguments': {key: ref, 'cursor': cursor}} if cursor else None,
                'reference': self._reference(doc, start, end),
                'evidence_ref': f'{doc.id}@{start}-{end}'}

    def read_section(self, section_ref, cursor=None):
        if not isinstance(section_ref, str) or '-H' not in section_ref:
            raise ValueError('需要实际目录返回的小节引用')
        doc_id, _ = section_ref.rsplit('-H', 1)
        doc = self._document(doc_id)
        section = next((s for s in doc.sections if s['section_ref'] == section_ref), None)
        if section is None:
            raise ValueError('小节引用不存在')
        start = section['start']
        if cursor is not None:
            token = self._decode(cursor, 'section')
            if token.get('ref') != section_ref:
                raise ValueError('游标不属于该小节')
            start = token.get('start')
        return {**self._read(doc, start, section['end'], READ_CHARS, 'section', section_ref, section['start']),
                'section_ref': section_ref, 'section_title': section['title'], 'section_path': list(section['path'])}

    def read_document(self, document_id, cursor=None, view='text', start=None, limit=None):
        if view not in {'text', 'outline', 'reference'}:
            raise ValueError('view 只接受 text、outline 或 reference')
        match = re.fullmatch(r'([^@]+)@(\d+)-(\d+)', document_id) if isinstance(document_id, str) else None
        doc = self._document(match[1] if match else document_id)
        if view == 'outline':
            if match or start is not None or limit is not None:
                raise ValueError('目录模式不接受命中引用或正文范围')
            offset = 0
            if cursor is not None:
                token = self._decode(cursor, 'outline')
                if token.get('ref') != doc.id:
                    raise ValueError('游标不属于该资料')
                offset = token.get('offset')
            return self._outline(doc, offset)
        if view == 'reference':
            if match is None or start is not None or limit is not None:
                raise ValueError('引用模式需要实际 evidence_ref，不接受 start/limit；程序在引用范围内分页')
            left, right = int(match[2]), int(match[3])
            reference = self.validate_reference(f'{doc.id}@{left}-{right}')
            requested = f'{doc.id}@{reference["start"]}-{reference["end"]}'
            position = left
            if cursor is not None:
                token = self._decode(cursor, 'reference')
                if token.get('ref') != requested:
                    raise ValueError('游标不属于该引用')
                position = token.get('start')
            result = self._read(doc, position, right, READ_CHARS, 'reference', requested, left)
            result.update(view='reference', requested_reference=requested)
            if result['continuation']:
                result['continuation']['arguments']['view'] = 'reference'
            return result
        size = READ_CHARS if limit is None else self._number(limit, 1, READ_CHARS)
        position = 0 if start is None else start
        if match:
            left, right = int(match[2]), int(match[3])
            self._number(left, 0, len(doc.content)-1)
            self._number(right, left+1, len(doc.content))
            before = min(500, max(0, size - (right-left)) // 2)
            position = max(0, left - before) if start is None else start
        if cursor is not None:
            if start is not None:
                raise ValueError('游标与 start 不能同时指定')
            token = self._decode(cursor, 'document')
            if token.get('ref') != doc.id:
                raise ValueError('游标不属于该资料')
            position = token.get('start')
        result = self._read(doc, position, len(doc.content), size, 'document', doc.id, 0)
        result['view'] = 'text'
        if match:
            result['anchor_ref'] = f'{doc.id}@{left}-{right}'
        result['previous_cursor'] = (self._cursor('document', ref=doc.id, start=max(0, position-READ_CHARS))
                                     if position else None)
        result['previous_read'] = ({'tool': 'read_document', 'arguments': {
            'document_id': doc.id, 'cursor': result['previous_cursor']}} if position else None)
        return result

    def list_documents(self, cursor=None, source_id=None, limit=INDEX_SIZE):
        self._number(limit, 1, 20)
        if source_id is not None and source_id not in self._sources.values():
            raise ValueError('来源编号不存在')
        until, offset = self._offset, 0
        if cursor is not None:
            token = self._decode(cursor, 'list')
            if source_id is not None and source_id != token.get('source_id'):
                raise ValueError('游标与来源范围不一致')
            source_id, until, offset = token.get('source_id'), token.get('until'), token.get('offset')
            self._number(until, 0, self._offset)
        docs = [d for d in self._docs.values() if int(d.record_id) <= until and (source_id is None or d.source_id == source_id)]
        self._number(offset, 0, len(docs))
        rows = [{**self._summary(d), 'preview': d.content[:200], 'query': d.query[:200]}
                for d in docs[offset:offset+limit]]
        end = offset + len(rows)
        next_cursor = self._cursor('list', until=until, offset=end, source_id=source_id) if end < len(docs) else None
        return {'operation': 'list_documents', 'status': 'success', 'documents': rows, 'next_cursor': next_cursor,
                'continuation': {'tool': 'list_documents', 'arguments': {
                    'cursor': next_cursor, 'limit': limit}} if next_cursor else None}

    def record_sources(self, record):
        """历史导航保留原阅读位置；预览与入口不作为正文交付。"""
        try:
            payload = json.loads(record['result']) if record.get('kind') == 'tool_completed' else {}
        except (ValueError, TypeError, KeyError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        rows = payload.get('matches', payload.get('results', [payload]))
        if payload.get('content_kind')=='structured_answer' and payload.get('schema_version')==1:
            rows=[{**row,**row['reference'],'view':'reference',
                   'requested_reference':f"{row['reference']['document_id']}@{row['reference']['start']}-{row['reference']['end']}"}
                  for row in payload.get('source_quotes',[]) if isinstance(row,dict) and isinstance(row.get('reference'),dict)]
        if not isinstance(rows, list):
            rows = []
        rows = [row for row in rows if isinstance(row, dict) and row.get('document_id') in self._docs]
        if not rows:
            rows = [{'document_id': doc_id} for doc_id in self._by_record.get(record['id'], ())]
        sources = []
        for row in rows:
            doc = self._docs[row['document_id']]
            operation = payload.get('operation')
            start, end = row.get('start'), row.get('end')
            has_range = (type(start) is int and type(end) is int and 0 <= start < end <= len(doc.content))
            preview_start, preview_end = (start, end) if has_range else (0, len(doc.content))
            source = {**self._summary(doc),
                      'record_operation': operation, 'read_status': row.get('status', payload.get('status')),
                      'preview': doc.content[preview_start:min(preview_start+200, preview_end)],
                      'read_action': {'tool': 'read_document', 'arguments': {'document_id': doc.id}}}
            for field in ('section_ref', 'section_title', 'section_path', 'requested_reference',
                          'view', 'match_ref', 'match_range', 'complete', 'reached_end', 'remaining_characters'):
                if field in row:
                    source[field] = deepcopy(row[field])
            if has_range:
                source.update(start=start, end=end, evidence_ref=f'{doc.id}@{start}-{end}')
            if operation == 'read_section' and row.get('section_ref'):
                source['read_action'] = {'tool': 'read_section', 'arguments': {'section_ref': row['section_ref']}}
            elif operation == 'read_document' or payload.get('content_kind')=='structured_answer':
                if row.get('view') == 'outline':
                    source['read_action']['arguments']['view'] = 'outline'
                elif has_range:
                    source['read_action']['arguments'] = {
                        'document_id': row.get('requested_reference') or source['evidence_ref'], 'view': 'reference'}
            elif operation == 'find_in_documents':
                source['lookup_query'] = payload.get('query', '')
                if row.get('match_ref'):
                    source['read_action']['arguments'] = {'document_id': row['match_ref'], 'view': 'text'}
            sources.append(source)
        return sources

    def describe_target(self, target, view, cursor=None, query=None):
        """检查稳定阅读入口并返回导航；局部校验取得的文本不计为模型已读。"""
        if not isinstance(target,str) or view not in {'section','text','reference','outline','find','page'}:
            raise ValueError('阅读目标需要实际引用和 section/text/reference/outline/find/page 模式')
        if query is not None and view!='find':
            raise ValueError('query 只用于 find 模式')
        if view=='page':
            if cursor is not None: raise ValueError('page 模式不接受游标')
            docs = [doc for doc in self._docs.values() if doc.source_id==target or doc.id==target]
            doc = docs[-1] if docs else None
            url = doc.requested_url if doc else target
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            if parsed.scheme not in {'http','https'} or not parsed.netloc:
                raise ValueError('page 目标需要已发现 source_id/document_id 或明确 HTTP(S) URL')
            return {'source_id':doc.source_id if doc else None,'document_id':None,
                    'title':doc.title if doc else url,'url':url,'start':None,'end':None,
                    'read_action':{'tool':'read_page','arguments':{'url':url}}}
        if view=='section':
            result = self.read_section(target, cursor=cursor)
            args = {'section_ref':target, **({'cursor':cursor} if cursor is not None else {})}
            action = {'tool':'read_section','arguments':args}
        elif view=='find':
            if not isinstance(query,str) or not query.strip(): raise ValueError('find 目标需要 query')
            self.find_in_documents(query,target,cursor=cursor)
            doc = self._document(target)
            result = {**self._summary(doc),'target_start':0,'target_end':len(doc.content)}
            action = {'tool':'find_in_documents','arguments':{'query':query,'document_id':target,
                       **({'cursor':cursor} if cursor is not None else {})}}
        else:
            result = self.read_document(target,cursor=cursor,view=view)
            action = {'tool':'read_document','arguments':{'document_id':target,'view':view,
                       **({'cursor':cursor} if cursor is not None else {})}}
        doc = self._document(result['document_id'])
        return {'source_id':doc.source_id,'document_id':doc.id,'title':result.get('section_title') or doc.title,
                'path':result.get('section_path',[]),'url':doc.url,
                'start':result.get('target_start',0),'end':result.get('target_end',len(doc.content)),
                'read_action':action}

    def select_quote(self, reference, text):
        """限定引用内定位原句；仅消除明确格式差异，返回保存文本的原始片段。"""
        ref=self.validate_reference(reference)
        doc=self._document(ref['document_id'])
        source=doc.content[ref['start']:ref['end']]
        position=source.find(text)
        if position>=0:
            if source.find(text,position+1)>=0:
                raise ValueError('引用中有多处相同原句，请缩小 reference 或补充能区分位置的措辞')
            left,right=position,position+len(text)
        else:
            def project(value):
                ignored=set()
                for match in re.finditer(r'(?<!!)\[([^\]\n]+)\]\([^\)\n]*\)',value):
                    ignored.update(range(match.start(),match.start(1)))
                    ignored.update(range(match.end(1),match.end()))
                for match in re.finditer(r'\\([\\`*_{}\[\]()#+\-.!>~|])',value):
                    ignored.add(match.start())
                chars,positions=[],[]
                for index,char in enumerate(value):
                    if index in ignored:continue
                    if char.isspace():
                        if chars and chars[-1]==' ':
                            positions[-1]=(positions[-1][0],index+1)
                            continue
                        char=' '
                    chars.append(char);positions.append((index,index+1))
                return ''.join(chars),positions
            rendered,positions=project(source)
            selected,_=project(text);selected=selected.strip()
            position=rendered.find(selected) if selected else -1
            if position<0:
                token=f"{doc.id}@{ref['start']}-{ref['end']}"
                raise ValueError(f'原句与保存原文不匹配；请用 read_document(document_id="{token}", view="reference") '
                                 '核对该范围，复制对应措辞或移除尚未核实的 evidence_quotes')
            if rendered.find(selected,position+1)>=0:
                raise ValueError('引用中有多处格式等价的文字，请缩小原句 reference 或补充能区分位置的措辞')
            left,right=positions[position][0],positions[position+len(selected)-1][1]
        original=source[left:right]
        if len(original)>400:
            raise ValueError('匹配的原文包含格式后超过400字符，请选择更短的关键语段')
        return {'reference':self._reference(doc,ref['start']+left,ref['start']+right),'text':original}

    def complete_section(self, reference):
        """仅当范围恰好等于保存文本中的完整小节时返回其信息。"""
        ref=self.validate_reference(reference)
        doc=self._document(ref['document_id'])
        section=next((row for row in doc.sections
                      if row['start']==ref['start'] and row['end']==ref['end']),None)
        return deepcopy(section) if section is not None else None

    def paragraph_for(self, reference):
        """按保存文本的空行寻找引用所在原段；名称只来自实际标题。"""
        ref=self.validate_reference(reference)
        doc=self._document(ref['document_id'])
        start=0
        end=len(doc.content)
        for boundary in re.finditer(r'\n[ \t]*\n',doc.content):
            if boundary.end()<=ref['start']:
                start=boundary.end()
            elif boundary.start()>=ref['end']:
                end=boundary.start()
                break
        # 列表项有明确原始标记；同级或更高层级的下一项结束当前项。
        items=[]
        fence=None
        offset=0
        for line in doc.content.splitlines(keepends=True):
            marker=re.match(r'^ {0,3}(`{3,}|~{3,})',line)
            if marker:
                if fence is None:fence=(marker[1][0],len(marker[1]))
                elif marker[1][0]==fence[0] and len(marker[1])>=fence[1]:fence=None
            elif fence is None and start<=offset<end:
                item=re.match(r'^( {0,3})(?:[-+*]|\d+[.)])[ \t]+',line)
                if item:items.append((offset,len(item[1])))
            offset+=len(line)
        preceding=[item for item in items if item[0]<=ref['start']]
        if preceding:
            position,level=preceding[-1]
            start=position
            end=next((position for position,indent in items if position>=ref['end'] and indent<=level),end)
        sections=[section for section in doc.sections
                  if section['start']<=ref['start'] and ref['end']<=section['end']]
        section=max(sections,key=lambda row:(row['level'],row['start'])) if sections else None
        return {**self._summary(doc),'reference':self._reference(doc,start,end),
                'text':doc.content[start:end], 'path':list(section['path']) if section else [],
                'read_action':{'tool':'read_document','arguments':{
                    'document_id':f'{doc.id}@{start}-{end}','view':'reference'}}}

    def unread_target(self, target, view, start, end, spans):
        """从已确认交付范围生成最早缺口的阅读入口，不获取或交付正文。"""
        if view not in {'section', 'text', 'reference'} or (view=='text' and '@' in target):
            return None
        position, gap_end = start, end
        for left, right in sorted(spans):
            if right <= position:
                continue
            if left > position:
                gap_end = min(left, end)
                break
            position = max(position, right)
            if position >= end:
                return None
        if position >= end:
            return None
        if view=='section':
            action = {'tool':'read_section', 'arguments':{'section_ref':target,
                      'cursor':self._cursor('section', ref=target, start=position)}}
        elif view=='reference':
            ref = self.validate_reference(target)
            target = f"{ref['document_id']}@{ref['start']}-{ref['end']}"
            action = {'tool':'read_document', 'arguments':{'document_id':target,'view':'reference',
                      'cursor':self._cursor('reference', ref=target, start=position)}}
        else:
            action = {'tool':'read_document', 'arguments':{'document_id':target,'view':'text',
                      'cursor':self._cursor('document', ref=target, start=position)}}
        return {'start':position, 'end':gap_end, 'read_action':action,
                'notice':'最早尚未确认交付的范围；该动作从此处开始，按原阅读目标分页。'}

    def find_in_documents(self, query, document_id=None, cursor=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError('query 需要 1–500 字符的非空查找文本')
        if document_id is not None:
            self._document(document_id)
        until, doc_offset, position = self._offset, 0, 0
        if cursor is not None:
            token = self._decode(cursor, 'find')
            if token.get('query') != query or token.get('document_id') != document_id:
                raise ValueError('游标与查找条件不一致')
            until, doc_offset, position = token.get('until'), token.get('doc_offset'), token.get('position')
            self._number(until, 0, self._offset)
        docs = [d for d in self._docs.values() if int(d.record_id) <= until and (document_id is None or d.id == document_id)]
        self._number(doc_offset, 0, len(docs))
        rows, next_cursor = [], None
        for index in range(doc_offset, len(docs)):
            doc = docs[index]
            self._number(position, 0, len(doc.content))
            while True:
                found = doc.content.find(query, position)
                if found < 0:
                    break
                if len(rows) == INDEX_SIZE:
                    next_cursor = self._cursor('find', query=query, document_id=document_id, until=until,
                                               doc_offset=index, position=found)
                    break
                end = found + len(query)
                section = next((s for s in reversed(doc.sections) if s['start'] <= found < s['end']), None)
                left, right = max(0, found-150), min(len(doc.content), end+250)
                rows.append({**self._summary(doc), 'match_ref': f'{doc.id}@{found}-{end}',
                             'match_range': {'start': found, 'end': end},
                             'start': left, 'end': right, 'content': doc.content[left:right],
                             'context_start': left, 'context_end': right,
                             'reference': self._reference(doc, left, right),
                             'evidence_ref': f'{doc.id}@{left}-{right}',
                             'read_action': {'tool': 'read_document', 'arguments': {
                                 'document_id': f'{doc.id}@{found}-{end}'}},
                             'section_ref': section['section_ref'] if section else None})
                position = end
            if next_cursor:
                break
            position = 0
        return {'operation': 'find_in_documents', 'status': 'success' if rows else 'empty',
                'query': query, 'document_id': document_id, 'matches': rows,
                'scan_complete': next_cursor is None, 'next_cursor': next_cursor,
                'continuation': {'tool': 'find_in_documents', 'arguments': {
                    'query': query, 'cursor': next_cursor,
                    **({'document_id': document_id} if document_id is not None else {})}} if next_cursor else None,
                'notice': '这是已保存文本的字面匹配；未命中不证明页面或现实中不存在相关事实。'}
