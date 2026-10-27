"""原文连续窗口：尊重空行与围栏，范围不足时显式保留完整目标。"""
import re


def _blocks(text, lower, upper):
    """空行分块；围栏内空行不拆块，表格和连续列表行自然保留。"""
    start, fence, offset = None, None, lower
    blocks = []
    for line in text[lower:upper].splitlines(keepends=True):
        end = offset + len(line)
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\n'))
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                blocks.append((start, end))
                start, fence = None, None
        elif marker:
            if start is not None:
                blocks.append((start, offset))
            start, fence = offset, (marker[1][0], len(marker[1]))
        elif not line.strip():
            if start is not None:
                blocks.append((start, offset))
                start = None
        elif start is None:
            start = offset
        offset = end
    if start is not None:
        blocks.append((start, upper))
    return blocks


def clip_match(text, lower, upper, start, end, limit):
    """包含完整命中；不足以容纳命中时只定位，截断时优先选行边界。"""
    if limit < end-start:
        return start, start
    if upper-lower <= limit:
        return lower, upper
    left = max(lower, min(start-(limit-(end-start))//2, upper-limit))
    right = min(upper, left+limit)
    if left > lower:
        boundary = text.find('\n', left, start)
        if boundary >= 0:
            left = boundary+1
    if right < upper:
        boundary = text.rfind('\n', end, right)
        if boundary >= 0:
            right = boundary+1
    return left, right


def find_window(text, start, end, lower, upper, limit):
    blocks = _blocks(text, lower, upper)
    covered = [i for i, (left, right) in enumerate(blocks) if left < end and start < right]
    if not covered:
        # 允许字面匹配跨空行；即使结构识别无结果也必须保留实际命中。
        return start, end, start, end
    first, last = covered[0], covered[-1]
    target_start, target_end = min(start, blocks[first][0]), max(end, blocks[last][1])
    if target_end-target_start <= limit:
        # 交替补充后文例外与前文前提；只添加完整相邻块，不跨小节。
        while True:
            changed = False
            if last+1 < len(blocks) and blocks[last+1][1]-target_start <= limit:
                last += 1
                target_end = blocks[last][1]
                changed = True
            if first > 0 and target_end-blocks[first-1][0] <= limit:
                first -= 1
                target_start = blocks[first][0]
                changed = True
            if not changed:
                break
    left, right = clip_match(text, target_start, target_end, start, end, limit)
    return left, right, target_start, target_end
