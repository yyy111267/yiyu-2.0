"""环节评测：15_industry_capabilities · 四领域能力包体系。"""

import asyncio
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from evaluation.stage.common import run_stage  # noqa: E402
from runtime.research_recipe import _ADAPTER_RECIPES  # noqa: E402


PACKAGES = {
    "digital_software_platform": "digital-software-platform",
    "semiconductor": "semiconductor",
    "advanced_manufacturing": "advanced-manufacturing",
    "consumer_brand": "consumer-brand",
}

VALUE_DIMENSIONS = {
    "business_essence", "demand_structure", "moat", "management_capital_allocation",
    "financial_quality", "growth_reinvestment", "valuation_safety_margin", "falsification",
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


async def execute(case_input: dict) -> dict:
    catalog = _load(ROOT / "toolkit/calc/metrics_catalog.yaml")
    supported = {item["metric_id"] for item in catalog.get("metrics", [])}
    adapter_catalog = _load(ROOT / "bus_router/adapters_catalog.yaml")
    business_models = _load(ROOT / "bus_router/business_model_overlays.yaml").get("models", [])
    active_adapters = [
        item for item in adapter_catalog.get("adapters", []) if item.get("status") == "active"
    ]
    manifests = {}
    recipes = {}
    rules = {}
    resources_exist = True

    for adapter_id, package_id in PACKAGES.items():
        package_dir = ROOT / "skills" / package_id
        manifest = _load(package_dir / "capability.yaml")
        recipe = _load(package_dir / "research_recipe.yaml")
        manifests[adapter_id] = manifest
        recipes[adapter_id] = recipe
        rules[adapter_id] = _load(package_dir / "industry_rules.yaml")

        for entrypoint in (manifest.get("entrypoints") or {}).values():
            resources_exist &= (package_dir / entrypoint).is_file()
        for value in (manifest.get("resources") or {}).values():
            for resource in value if isinstance(value, list) else [value]:
                resources_exist &= (ROOT / resource).is_file()

    return {
        "package_count": len(manifests),
        "adapter_ids": sorted(manifests),
        "all_capabilities": all(item.get("kind") == "capability" for item in manifests.values()),
        "all_ids_match": all(
            manifest.get("id") == PACKAGES[adapter_id]
            and manifest.get("adapter_id") == adapter_id
            for adapter_id, manifest in manifests.items()
        ),
        "all_dynamic_in_deep_research": all(
            "deep-research" in (manifest.get("activation") or {}).get("workflows", [])
            and adapter_id in (manifest.get("activation") or {}).get("business_groups", [])
            for adapter_id, manifest in manifests.items()
        ),
        "all_depend_on_metric": all(
            "metric-calculation" in (item.get("depends_on") or [])
            for item in manifests.values()
        ),
        "all_depend_on_value_core": all(
            "value-investing-core" in (item.get("depends_on") or [])
            for item in manifests.values()
        ),
        "resources_exist": resources_exist,
        "all_recipe_metrics_supported": all(
            set(recipe.get("metric_ids") or []) <= supported for recipe in recipes.values()
        ),
        "all_packaged_recipes_loaded": all(
            _ADAPTER_RECIPES.get(adapter_id) == recipe
            for adapter_id, recipe in recipes.items()
        ),
        "all_value_dimensions_complete": all(
            set(recipe.get("value_dimensions") or {}) == VALUE_DIMENSIONS
            for recipe in recipes.values()
        ),
        "domain_adapter_ids": sorted(
            item["adapter_id"] for item in active_adapters if item.get("kind") != "fallback"
        ),
        "fallback_adapter_id": next(
            (item["adapter_id"] for item in active_adapters if item.get("kind") == "fallback"), ""
        ),
        "generic_has_no_industry_package": "generic" not in {
            item.get("adapter_id") for item in manifests.values()
        },
        "business_model_count": len(business_models),
        "business_model_ids": sorted(item.get("id") for item in business_models),
        "all_reject_universal_thresholds": all(
            rule.get("universal_thresholds", {}).get("allowed") is False
            for rule in rules.values()
        ),
        "digital_usage_ndr_applicable": rules["digital_software_platform"]["usage_api_ndr_applicable"],
        "digital_conglomerate_requires_split": rules["digital_software_platform"]["subtypes"]["conglomerate"]["requires_unit_split"],
        "semi_fabless_utilization": rules["semiconductor"]["subtypes"]["fabless"]["capacity_utilization_applicable"],
        "semi_idm_utilization": rules["semiconductor"]["subtypes"]["idm_foundry"]["capacity_utilization_applicable"],
        "semi_qualification_stages": rules["semiconductor"]["qualification_stages"],
        "manufacturing_subtypes": sorted(rules["advanced_manufacturing"]["subtypes"]),
        "manufacturing_integration_utilization": rules["advanced_manufacturing"]["subtypes"]["system_integration"]["capacity_utilization_applicable"],
        "manufacturing_components_utilization": rules["advanced_manufacturing"]["subtypes"]["precision_components"]["capacity_utilization_applicable"],
        "manufacturing_commercial_stages": rules["advanced_manufacturing"]["commercial_stages"],
    }


if __name__ == "__main__":
    asyncio.run(run_stage(__file__, execute))
