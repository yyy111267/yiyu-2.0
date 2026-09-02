"""环节评测：14_consumer_brand · 消费品牌行业能力包。"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402
from runtime.preloop.orchestrator import collect_adapter_questions  # noqa: E402
from runtime.research_recipe import _ADAPTER_RECIPES, build_research_recipe  # noqa: E402


async def execute(case_input: dict) -> dict:
    package_dir = ROOT / "skills" / "consumer-brand"
    manifest = yaml.safe_load((package_dir / "capability.yaml").read_text(encoding="utf-8"))
    recipe = yaml.safe_load((package_dir / "research_recipe.yaml").read_text(encoding="utf-8"))
    rules = yaml.safe_load((package_dir / "industry_rules.yaml").read_text(encoding="utf-8"))
    catalog = yaml.safe_load((ROOT / "toolkit/calc/metrics_catalog.yaml").read_text(encoding="utf-8"))

    resource_paths = []
    for value in (manifest.get("resources") or {}).values():
        resource_paths.extend(value if isinstance(value, list) else [value])
    supported = {item["metric_id"] for item in catalog.get("metrics", [])}
    packaged_recipe = _ADAPTER_RECIPES.get("consumer_brand") or {}
    fallback_profile = SimpleNamespace(
        selected_adapter="consumer_brand", secondary_adapter="", adapter_fallback=True,
        company_profile=SimpleNamespace(
            development_stage=SimpleNamespace(value="待核验"),
        ),
    )
    fallback_recipe = build_research_recipe([fallback_profile])

    return {
        "kind": manifest.get("kind"),
        "adapter_id": manifest.get("adapter_id"),
        "depends_on": manifest.get("depends_on") or [],
        "resources_exist": all((ROOT / path).is_file() for path in resource_paths),
        "recipe_metrics_supported": set(recipe.get("metric_ids") or []) <= supported,
        "packaged_recipe_loaded": packaged_recipe == recipe,
        "channel_operator": rules["channel_stuffing"]["operator"],
        "channel_min_signals": rules["channel_stuffing"]["minimum_independent_signals"],
        "inventory_contexts": sorted(rules["inventory_contexts"]),
        "universal_thresholds_allowed": rules["universal_thresholds"]["allowed"],
        "fallback_uses_generic_recipe": fallback_recipe["adapters"] == []
            and fallback_recipe["metric_ids"] == ["roic", "fcf", "pe_ttm"],
        "fallback_has_no_consumer_questions": collect_adapter_questions([fallback_profile]) == [],
    }


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
