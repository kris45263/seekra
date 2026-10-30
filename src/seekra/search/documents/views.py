"""一次预算交付的正文视图；复用 store 的引用和游标规则。"""
from copy import deepcopy
from .text_windows import clip_match


_IDENTITY = ('source_id', 'document_id', 'title', 'url', 'requested_url',
             'content_kind', 'total_characters', 'record_id', 'retrieved_at',
             'snapshot_id', 'query', 'duplicate_of')



def _identity(row):
    return {key: deepcopy(row[key]) for key in _IDENTITY if key in row}


class DeliveryView:
    """单次交付的可缩减视图；游标和范围规则属于资料实现。"""
    def __init__(self, payload, documents, arguments):
        self.payload, self.documents, self.arguments = payload, documents, arguments
        self.operation = payload.get('operation')
        self.rows_key = {'tavily_search': 'results', 'find_in_documents': 'matches',
                         'list_documents': 'documents'}.get(self.operation)
        self.rows = payload.get(self.rows_key, []) if self.rows_key else []
        self.outline = payload.get('outline', [])
        self.row_count, self.outline_count = len(self.rows), len(self.outline)

    def _body(self, row, share):
        doc = self.documents._document(row['document_id'])
        start, stop = row['start'], row.get('target_end', len(doc.content))
        lower = row.get('target_start', 0)
        if self.operation == 'read_section':
            kind, ref = 'section', row['section_ref']
        elif row.get('view') == 'reference':
            kind, ref = 'reference', row['requested_reference']
        else:
            kind, ref = 'document', doc.id
        size = min(share, len(row['content']))
        if size:
            result = self.documents._read(doc, start, stop, size, kind, ref, lower)
            result = {key: value for key, value in result.items()
                      if key not in {'question', 'run_status', 'retrieved_date'}}
        else:
            cursor = self.documents._cursor(kind, ref=ref, start=start)
            key = 'section_ref' if kind == 'section' else 'document_id'
            result = {**_identity(row), 'operation': self.operation, 'status': 'partial',
                      'content': '', 'start': start, 'end': start,
                      'target_start': lower, 'target_end': stop, 'complete': False,
                      'reached_end': False, 'remaining_characters': stop-start,
                      'next_start': start, 'next_cursor': cursor,
                      'continuation': {'tool': 'read_section' if kind == 'section' else 'read_document',
                                       'arguments': {key: ref, 'cursor': cursor}},
                      'not_displayed': True}
        result['operation'] = self.operation
        for key in ('section_ref', 'section_title', 'section_path', 'view',
                    'requested_reference', 'anchor_ref'):
            if key in row:
                result[key] = deepcopy(row[key])
        if kind == 'reference' and result['continuation']:
            result['continuation']['arguments']['view'] = 'reference'
        return result

    def _row(self, row, share):
        result = _identity(row)
        result['read_action'] = {'tool': 'read_document', 'arguments': {'document_id': row['document_id']}}
        if self.operation == 'list_documents':
            preview = row.get('preview', '')[:share]
            result.update(preview=preview, preview_truncated=len(preview) < len(row.get('preview', '')))
            return result
        start, end = row['start'], row['end']
        if self.operation == 'find_in_documents':
            match = row['match_range']
            # 命中词不拆开：分配不足时仅保留定位信息，不伪造已读正文引用。
            left, right = clip_match(self.documents._document(row['document_id']).content,
                                     start, end, match['start'], match['end'], share)
            result.update(match_range=deepcopy(match), match_ref=row['match_ref'],
                          section_ref=row.get('section_ref'),
                          read_action=deepcopy(row['read_action']))
        else:
            left, right = start, min(end, start+share)
        doc = self.documents._document(row['document_id'])
        content = doc.content[left:right]
        result.update(content=content, start=left, end=right,
                      context_start=left, context_end=right,
                      truncated=left > start or right < end or row.get('truncated', False))
        target_start = row.get('target_start', start) if self.operation == 'find_in_documents' else start
        target_end = row.get('target_end', end) if self.operation == 'find_in_documents' else len(doc.content)
        result.update(target_start=target_start, target_end=target_end,
                      complete=left == target_start and right == target_end,
                      remaining_characters=target_end-right, not_displayed_before=left-target_start)
        if self.operation == 'find_in_documents':
            context_ref = f'{doc.id}@{target_start}-{target_end}'
            result['read_context_action'] = {'tool': 'read_document', 'arguments': {
                'document_id': context_ref, 'view': 'reference'}}
        else:
            cursor = self.documents._cursor('document', ref=doc.id, start=right) if right < target_end else None
            result['continuation'] = {'tool': 'read_document', 'arguments': {
                'document_id': doc.id, 'cursor': cursor}} if cursor else None
        if content:
            result.update(reference=self.documents._reference(doc, left, right),
                          evidence_ref=f'{doc.id}@{left}-{right}')
        else:
            result['not_displayed'] = True
        return result

    def _until(self):
        kind = 'list' if self.operation == 'list_documents' else 'find'
        cursor = self.arguments.get('cursor') or self.payload.get('next_cursor')
        if cursor:
            return self.documents._decode(cursor, kind)['until']
        return max((int(row['record_id']) for row in self.rows), default=self.documents._offset)

    def _omitted(self, result):
        omitted = self.rows[self.row_count:]
        if not omitted:
            return
        result['not_displayed'] = {'rows': len(omitted),
                                  'document_ids': list(dict.fromkeys(row['document_id'] for row in omitted))}
        if self.operation == 'list_documents':
            token = self.documents._decode(self.arguments['cursor'], 'list') if self.arguments.get('cursor') else {}
            cursor = self.documents._cursor('list', until=self._until(),
                     offset=token.get('offset', 0)+self.row_count, source_id=token.get('source_id', self.arguments.get('source_id')))
            result.update(next_cursor=cursor, continuation={'tool': 'list_documents',
                          'arguments': {'cursor': cursor, 'limit': self.arguments.get('limit', 10)}})
        elif self.operation == 'find_in_documents':
            until = self._until()
            docs = [doc for doc in self.documents._docs.values() if int(doc.record_id) <= until
                    and (self.payload.get('document_id') is None or doc.id == self.payload['document_id'])]
            first = omitted[0]
            cursor = self.documents._cursor('find', query=self.payload['query'],
                     document_id=self.payload.get('document_id'), until=until,
                     doc_offset=next(i for i, doc in enumerate(docs) if doc.id == first['document_id']),
                     position=first['match_range']['start'])
            result.update(scan_complete=False, next_cursor=cursor,
                          continuation={'tool': 'find_in_documents', 'arguments': {
                              'query': self.payload['query'], 'cursor': cursor,
                              **({'document_id': self.payload['document_id']} if self.payload.get('document_id') else {})}})
        else:
            result['not_displayed']['read_actions'] = [
                {'tool': 'read_document', 'arguments': {'document_id': ref}}
                for ref in result['not_displayed']['document_ids']]

    def render(self, share):
        payload = self.payload
        if self.rows_key:
            result = {key: deepcopy(payload[key]) for key in ('operation', 'status', 'content_kind',
                      'query', 'document_id', 'scan_complete', 'next_cursor', 'continuation') if key in payload}
            result[self.rows_key] = [self._row(row, share) for row in self.rows[:self.row_count]]
            self._omitted(result)
        else:
            result = self._body(payload, share) if 'content' in payload else _identity(payload)
            result.setdefault('operation', self.operation)
            result.setdefault('status', payload.get('status', 'success'))
        if self.outline or 'outline_available' in payload:
            doc = self.documents._document(payload['document_id'])
            rows = []
            for row in self.outline[:self.outline_count]:
                item = {key: deepcopy(row[key]) for key in ('section_ref', 'title', 'path', 'level',
                        'start', 'end', 'characters') if key in row}
                item.update(preview=row.get('preview', '')[:share], preview_start=row['preview_start'],
                            preview_end=row['preview_start']+min(share, len(row.get('preview', ''))))
                rows.append(item)
            result.update(outline=rows, outline_available=bool(doc.sections))
            omitted = self.outline[self.outline_count:]
            cursor = payload.get('outline_cursor', payload.get('next_cursor'))
            if omitted:
                offset = next(i for i, section in enumerate(doc.sections) if section['section_ref'] == omitted[0]['section_ref'])
                cursor = self.documents._cursor('outline', ref=doc.id, offset=offset)
                result['outline_not_displayed'] = len(omitted)
            action = {'tool': 'read_document', 'arguments': {'document_id': doc.id, 'view': 'outline', 'cursor': cursor}} if cursor else None
            if 'content' in payload:
                result.update(outline_cursor=cursor, outline_continuation=action)
            else:
                result.update(next_cursor=cursor, continuation=action)
        for key in ('notice', 'coverage', 'final_url'):
            if key in payload:
                result[key] = deepcopy(payload[key])
        result['delivery_notice'] = '本批按展示预算分配正文；引用仅覆盖实际展示的文字。未展示资料保存在原快照，可按读取或续读入口回查。'
        return result

    def trim_navigation(self):
        if self.outline_count:
            self.outline_count -= 1
            return True
        if self.row_count:
            self.row_count -= 1
            return True
        return False


    @property
    def omitted_rows(self):
        return len(self.rows) - self.row_count

    @property
    def omitted_outline_rows(self):
        return len(self.outline) - self.outline_count
