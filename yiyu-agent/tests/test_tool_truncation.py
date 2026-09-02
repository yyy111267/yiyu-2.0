"""工具结果截断必须保结构。

压成字符串会让所有按字段读取证据的逻辑失效（取数门禁读不到 value、
引用解析读不到 source），因此超长时只能裁剪字符串叶子。
"""
from toolkit.base import ReadOnlyTool, ToolSchema


class _TinyTool(ReadOnlyTool):
    schema = ToolSchema(name="test.tiny", description="", parameters={}, max_chars=200)

    async def execute(self, **kwargs) -> dict:
        return _fat_result()


def _fat_result() -> dict:
    return {
        "success": True,
        "value": 19.77,
        "status": "ok",
        "field_evidence": {
            f"f{i}": {"field": f"f{i}", "source": "westock" * 20, "as_of": "2026-08-31"}
            for i in range(20)
        },
    }


def test_short_result_untouched():
    result, truncated = _TinyTool().truncate_result({"value": 1.0})
    assert not truncated
    assert result == {"value": 1.0}


def test_truncate_keeps_dict_shape_and_value():
    tool = _TinyTool()
    result, truncated = tool.truncate_result(_fat_result())
    assert truncated
    assert isinstance(result, dict), "结构化结果被压成字符串，下游读不到字段"
    assert result["value"] == 19.77
    assert len(str(result)) <= tool.schema.max_chars
