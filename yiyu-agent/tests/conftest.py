"""全局测试资源回收。

实体解析的 A 股索引使用 aiosqlite 常驻连接；其工作线程是非 daemon。pytest
结束前必须显式停止，否则断言已完成但 Python 进程会卡在解释器的线程回收阶段。
"""

from toolkit.entity.resolver import _close_ashare_singleton_sync


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    _close_ashare_singleton_sync()
