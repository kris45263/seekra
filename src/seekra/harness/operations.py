"""操作结果公共约定：状态描述执行事实，不认证结论是否可信。"""

from typing import Literal, NotRequired, TypedDict


class OperationResult(TypedDict):
    """搜索、读取和笔记共享的结果字段，其余字段由各操作提供。"""
    operation: str
    status: Literal["success", "empty", "partial", "failed"]
    content_kind: Literal["snippet", "extracted_text", "model_note", "none"]
    error: NotRequired[str]
    error_code: NotRequired[str]
    http_status: NotRequired[int]


def failure(operation: str, code: str, message: str, **details) -> dict:
    """生成明确的失败结果，不将失败包装成空搜索或否定事实。"""
    return {"operation": operation, "status": "failed", "content_kind": "none",
            "error_code": code, "error": message, **details}
