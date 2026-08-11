"""三条硬规则回归测试（骨架）。"""

import pytest


@pytest.mark.asyncio
async def test_methodology_requires_confirm():
    """未确认的候选条目不得进入方法库、不得参与回答。"""
    # TODO
    assert True


@pytest.mark.asyncio
async def test_personal_overrides_standard():
    """冲突时结论遵循个人方法论，且必须说明与标准框架的差异。"""
    # TODO
    assert True


@pytest.mark.asyncio
async def test_mirror_test_gate():
    """镜子测试 5 句话不完整时，不给买入结论。"""
    # TODO
    assert True
