"""已公开的 Search Python 入口；新调用使用 seekra.search.agent。"""
from .search.agent import run_agent, execute_run

__all__ = ['run_agent', 'execute_run']
