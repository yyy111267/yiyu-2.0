"""preloop —— 公司研究循环前处理（PRD 5.3）。

四件套：company_facts → granularity_decision → company_profile/Adapter → research_plan。
由 orchestrator.run_preloop 编排，产物喂给 runtime.loop.AgentLoop。
"""

from .orchestrator import (
    PreloopOutput,
    build_initial_context,
    collect_adapter_questions,
    entity_to_current_schema,
    facts_to_plan_input,
    run_preloop,
)
from runtime.research_recipe import build_research_recipe, recipe_prompt_block

__all__ = [
    "PreloopOutput",
    "build_initial_context",
    "collect_adapter_questions",
    "entity_to_current_schema",
    "facts_to_plan_input",
    "run_preloop",
    "build_research_recipe",
    "recipe_prompt_block",
]
