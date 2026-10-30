"""搜索执行历史的索引、回读与响应当时的证据归属。"""
import json


def history_index(session, cursor=0, limit=20):
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('索引范围不合法')
    rows = []
    for index in range(max(cursor, session._history_start), len(session.records)):
        record = session.records[index]
        if record['kind'] not in {'run_started','model_completed','tool_completed','run_ended'}:
            continue
        row = {'record_id':record['id'], 'kind':record['kind'], 'run_id':record.get('run_id')}
        details = session.history_details(record)
        row.update({key: value[:120] if key=='question' else value for key,value in details.items()})
        for name in ['question','status','call_id','tool_name']:
            if name in record:
                row[name] = str(record[name])[:120]
        if record['kind'] == 'model_completed':
            row['review_action'] = {'tool':'read_history', 'arguments':{
                'record_id':record['id'], 'view':'review'}}
        rows.append(row)
        if len(rows) > limit:
            break
    selected = rows[:limit]
    return {'operation':'list_history','status':'success','content_kind':'session_index','records':selected,
            'next_cursor':int(selected[-1]['record_id']) if len(rows)>limit else None}


def _review_origin(session, record):
    """仅使用该响应之前的同任务记录；后续进展或同 URL 新快照不改写归属。"""
    before = session.records[session._history_start:int(record['id'])]
    prior = [r for r in before
             if r.get('run_id') == record.get('run_id')]
    question = next(r for r in prior if r['kind'] == 'run_started')
    research, sources, referenced = None, {}, set()
    for entry in prior:
        if entry['kind'] != 'tool_completed':
            continue
        if entry.get('tool_name') == 'update_research':
            payload = json.loads(entry['result'])
            if (isinstance(payload, dict) and payload.get('operation') == 'update_research'
                    and payload.get('status') == 'success'):
                research = {'record_id':entry['id'], 'revision':payload.get('revision'),
                            'read_action':{'tool':'read_history','arguments':{
                                'record_id':entry['id'], 'view':'text'}}}
                state = payload.get('state', {})
                referenced = {ref['document_id'] for row in state.get('findings', [])+state.get('gaps', [])
                              if isinstance(row, dict) for ref in row.get('references', [])
                              if isinstance(ref, dict) and ref.get('document_id')}
    for entry in before:
        if entry['kind'] != 'tool_completed':
            continue
        for source in session.documents.record_sources(entry):
            if entry.get('run_id') != record['run_id'] and source['document_id'] not in referenced:
                continue
            sources[source['document_id']] = {
                **{key:source[key] for key in ('source_id','document_id','snapshot_id','record_id',
                                              'title','url','content_kind','retrieved_at') if key in source},
                'read_action':{'tool':'read_document','arguments':{'document_id':source['document_id']}}}
    return {'response_record_id':record['id'], 'run_id':record['run_id'],
            'response_time':record.get('time'), 'finish_reason':record.get('finish_reason'),
            'question_action':{'tool':'read_history','arguments':{'record_id':question['id'],'view':'text'}},
            'research_at_response':research, 'source_snapshots':list(sources.values())[:8],
            'omitted_source_count':max(0,len(sources)-8),
            'source_record_range':{'from':question['id'],'through':record['id']},
            'source_history_action':{'tool':'list_history','arguments':{
                'cursor':int(question['id'])-1,'limit':50}}}


def read_history(session, record_id, start=0, limit=6000, view='text'):
    """text 读消息，review 绑定当时归属；raw 保留原事件 JSON 的偏移含义。"""
    if view == 'raw':
        result = session.read_record(record_id, start=start, limit=limit)
        result.update(view='raw', offset_basis='event_json')
    elif view == 'review':
        record = session.record_entry(record_id)
        if record['kind'] != 'model_completed':
            raise ValueError('review 只接受 model_completed 记录；进展事件用 text 或 raw 回查')
        if type(start) is not int or type(limit) is not int or start < 0 or not 1 <= limit <= 24000:
            raise ValueError('读取范围不合法')
        message = record['message']
        text = message.get('content') or ''
        if message.get('tool_calls'):
            text += '\n工具调用：'+json.dumps(message['tool_calls'],ensure_ascii=False)
        if start >= len(text):
            raise ValueError('起点超出记录视图范围')
        end = min(start+limit,len(text))
        details = session.history_details(record)
        result = {'operation':'read_history', 'status':'success' if start == 0 and end == len(text) else 'partial',
                  **details, 'question':details.get('question','')[:200],
                  'record_id':record_id, 'record_kind':record['kind'],
                  'content_kind':'history_review', 'view':'review', 'offset_basis':'model_content_v1',
                  'content':text[start:end], 'start':start, 'end':end, 'total_characters':len(text),
                  'next_start':end if end < len(text) else None, 'origin':_review_origin(session,record),
                  'notice':'核对对象是本条历史模型消息。origin 关联响应产生前的同任务执行事实；'
                           'research_at_response 是当时已提交进展，不是当前核对进展，也不证明判断正确。'
                           'source_snapshots 是该任务此前获取、读取或当时进展引用的资料导航，不证明响应实际看到全部正文或支持全部断言。'
                           '核对原文主张用 read_document；核对保存事件用当时进展记录；核对用户要求用原问题。'
                           '来源超出八份时按 source_history_action 翻页，仅核对 source_record_range 内同任务记录。'}
    elif view == 'text':
        record = session.record_entry(record_id)
        if type(start) is not int or type(limit) is not int or start < 0 or not 1 <= limit <= 24000:
            raise ValueError('读取范围不合法')
        details = session.history_details(record)
        sources = session.documents.record_sources(record)
        try:
            tool_payload = json.loads(record['result']) if record['kind'] == 'tool_completed' else None
        except (ValueError, TypeError, KeyError):
            tool_payload = record.get('result')
        lines = [f"记录 {record_id}：{record['kind']}", f"取得时间：{record.get('time', '未记录')}"]
        if details:
            lines.append(f"原任务问题：{details['question'][:200]}")
        if sources:
            if isinstance(tool_payload,dict) and tool_payload.get('content_kind')=='structured_answer':
                lines.extend(['程序组织的终稿；原文来自保存资料，场景判断与建议由模型提出：',tool_payload['answer']])
            lines.append('该记录关联已保存资料；下列预览用于导航，需要原文时使用对应的 read_document 入口。')
            if isinstance(tool_payload, dict) and tool_payload.get('query'):
                lines.append(f"该操作的查询：{tool_payload['query']}")
            for source in sources:
                lines.extend([f"{source['document_id']} | {source['title']} | {source['url']}",
                              f"资料类型：{source['content_kind']}；资料获取查询：{source['query']}",
                              f"预览：{source['preview']}"])
                if source.get('section_ref'):
                    lines.append(f"小节：{source['section_ref']} | {source.get('section_title', '')}")
                if source.get('evidence_ref'):
                    lines.append(f"原工具结果的引用：{source['evidence_ref']}；读取状态：{source.get('read_status', '未记录')}")
        elif record['kind'] == 'run_started':
            lines.extend(['原始用户问题：', record['question']])
        elif record['kind'] == 'model_completed':
            message = record['message']
            lines.extend(['历史模型消息原文（模型生成，需核实）：', message.get('content') or ''])
            for call in message.get('tool_calls', []):
                lines.append(f"工具调用 {call['function']['name']}：{call['function']['arguments']}")
        elif record['kind'] == 'tool_completed':
            lines.append(f"工具：{record.get('tool_name', '未记录')}")
            payload = tool_payload
            if isinstance(payload, dict):
                if payload.get('content_kind') == 'research_state':
                    lines.append('历史模型研究记录；不是当前任务进展，保存成功不认证结论。')
                    lines.append(json.dumps(payload['state'], ensure_ascii=False, indent=2))
                elif payload.get('content_kind')=='structured_answer':
                    lines.extend(['程序组织的终稿；引用校验不认证判断：',payload['answer']])
                elif payload.get('content_kind') == 'model_note':
                    lines.extend(['临时模型笔记原文：', str(payload.get('note', ''))])
                else:
                    lines.append(json.dumps(payload, ensure_ascii=False))
            else:
                lines.append(str(payload))
        else:
            # 请求和投影的详细 system/replacements 可以显式查看 raw。
            lines.extend(f'{key}：{record[key]}' for key in ('round', 'status', 'error', 'error_code',
                         'usage', 'request_characters', 'checkpoint_record_id', 'partial_content', 'content')
                         if key in record and record[key] is not None)
            if record.get('answer_record_id'):
                answer_record=session.record_entry(record['answer_record_id'])
                payload=json.loads(answer_record['result'])
                lines.extend(['程序组织的终稿；引用校验不认证模型判断：',payload[answer_record['final_output_field']]])
        text = '\n'.join(lines)
        if start >= len(text):
            raise ValueError('起点超出记录视图范围')
        end = min(start+limit, len(text))
        result = {'operation': 'read_history', 'status': 'success' if start == 0 and end == len(text) else 'partial',
                  **details, 'question': details.get('question', '')[:200],
                  'record_id': record_id, 'record_kind': record['kind'], 'content_kind': 'history_text',
                  'view': 'text', 'offset_basis': 'event_text_v1', 'content': text[start:end],
                  'start': start, 'end': end, 'total_characters': len(text),
                  'next_start': end if end < len(text) else None,
                  'sources': [{k:row[k] for k in ('source_id', 'document_id', 'title', 'url', 'content_kind', 'read_action',
                               'section_ref', 'section_title', 'start', 'end', 'evidence_ref', 'requested_reference',
                               'view', 'match_ref', 'match_range', 'lookup_query', 'read_status', 'complete',
                               'remaining_characters') if k in row}
                              for row in sources],
                  'notice': '原始消息、状态和资料导航视图；历史模型文字不认证事实。偏移仅属于本视图，资料正文用资料引用读取。'}
        if record['kind'] == 'model_completed':
            result['review_action'] = {'tool':'read_history','arguments':{'record_id':record_id,'view':'review'}}
    else:
        raise ValueError('view 只接受 text、review 或 raw')
    result['continuation'] = ({'tool': 'read_history', 'arguments': {
        'record_id': record_id, 'view': view, 'start': result['next_start'], 'limit': limit}}
        if result['next_start'] is not None else None)
    return result
