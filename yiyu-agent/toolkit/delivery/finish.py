"""
delivery.finish —— 显式终止工具（信息充分性闸门）

把「结束」从隐式动作（无 tool_calls 即结束）改成显式动作：
LLM 必须调用本工具并完成信息自检，才能提交研究结论。

与 delivery.submit_conclusion 的分工：
  - finish：判断「信息够不够」——实体锁定、画像分类、关键数值已取数、维度覆盖。
  - submit_conclusion：判断「结论合不合规」——硬规则 R1–R5。
两层叠加，先过 finish（充分性），再过 submit_conclusion（合规性）。

失败返回 success=False 并附原因，loop 会拦截结束并让 LLM 继续补齐信息。
"""

from typing import Any

from toolkit.base import Tool, ToolSchema

FINISH_SCHEMA = {
    "type": "object",
    "properties": {
        "conclusion": {
            "type": "string",
            "description": "最终研究结论全文（面向小白用户的通俗表述）",
        },
        "self_check": {
            "type": "object",
            "properties": {
                "entity_locked": {
                    "type": "boolean",
                    "description": "研究对象是否已唯一锁定（含代码/市场/币种）",
                },
                "classified": {
                    "type": "boolean",
                    "description": "是否已完成公司画像与商业模式分类",
                },
                "data_sourced": {
                    "type": "boolean",
                    "description": "关键数值是否均来自工具取数返回值，而非模型心算",
                },
                "dimensions_covered": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "已覆盖的研究维度，如：生意/财务/行业/风险/估值",
                },
                "gaps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "未解决的数据缺口（结论中须披露；无则空数组）",
                },
            },
            "required": [
                "entity_locked",
                "classified",
                "data_sourced",
                "dimensions_covered",
                "gaps",
            ],
        },
    },
    "required": ["conclusion", "self_check"],
}


class FinishTool(Tool):
    """显式终止：只有信息足以支撑结论时才允许结束研究。"""

    schema = ToolSchema(
        name="delivery.finish",
        description=(
            "结束研究并提交最终结论。调用前必须完成信息自检（self_check）："
            "研究对象已唯一锁定、画像/分类已完成、关键数值已通过工具取数、"
            "核心研究维度已覆盖。若信息不足，请继续调用其他工具补齐后再调用本工具；"
            "未通过自检时本工具会拦截，不会结束研究。"
        ),
        parameters=FINISH_SCHEMA,
        read_only=True,  # 不写外部存储，仅结束循环并交由 loop 做合规校验
    )

    async def execute(self, conclusion: str, self_check: dict, **kwargs: Any) -> dict:
        reasons = []
        if not isinstance(self_check, dict):
            return {
                "success": False,
                "finish_allowed": False,
                "reason": "self_check 必须为对象（entity_locked/classified/data_sourced/dimensions_covered/gaps）",
            }

        if not self_check.get("entity_locked"):
            reasons.append("研究对象未唯一锁定（请先完成实体识别/消歧）")
        if not self_check.get("classified"):
            reasons.append("未完成公司画像与商业模式分类（请先调用画像/分类工具）")
        if not self_check.get("data_sourced"):
            reasons.append("关键数值未经工具取数（结论中的数字必须可溯源到工具返回值）")
        dims = self_check.get("dimensions_covered") or []
        if not dims:
            reasons.append("未声明已覆盖的核心研究维度（dimensions_covered 为空）")

        if reasons:
            return {
                "success": False,
                "finish_allowed": False,
                "reason": "；".join(reasons),
            }

        return {
            "success": True,
            "finish_allowed": True,
            "conclusion": conclusion,
            "self_check": self_check,
        }
