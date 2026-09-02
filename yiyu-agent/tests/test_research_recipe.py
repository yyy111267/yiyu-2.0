from types import SimpleNamespace

from runtime.research_recipe import build_research_recipe, recipe_prompt_block


def _profile(adapter: str, stage: str = "成熟", charging: str = "一次性销售"):
    return SimpleNamespace(
        selected_adapter=adapter,
        company_profile=SimpleNamespace(
            development_stage=SimpleNamespace(value=stage),
            charging=SimpleNamespace(value=charging),
        ),
    )


def test_early_digital_recipe_uses_revenue_valuation_not_profit_metrics():
    recipe = build_research_recipe([
        _profile("digital_software_platform", "商业化验证", "按量计费")
    ])

    assert recipe["metric_ids"] == ["ps_ttm", "ev_revenue", "runway", "fcf"]
    assert {"pe_ttm", "roe", "roic", "fcf_yield"} <= set(recipe["invalid_metrics"])
    assert recipe["business_models"] == ["usage_api"]
    assert "usage_pricing_and_variable_cost" in recipe["required_documents"]
    assert "关键数据表" in recipe_prompt_block(recipe)
    assert "收入估值锚" in recipe["valuation_note"]


def test_recipe_merges_adapters_without_creating_company_specific_path():
    recipe = build_research_recipe([
        _profile("consumer_brand"),
        _profile("advanced_manufacturing"),
    ])

    assert recipe["adapters"] == ["consumer_brand", "advanced_manufacturing"]
    assert {"roic", "fcf", "pe_ttm", "capex_intensity", "ev_ebitda"} <= set(recipe["metric_ids"])
    assert recipe["report_tables"] == ["key_metrics", "valuation", "peer_comparison"]


def test_new_listing_is_an_explicit_overlay_not_a_name_heuristic():
    recipe = build_research_recipe([_profile("consumer_brand")], event_overlays=["new_listing"])

    assert {"prospectus", "listing_announcement", "post_listing_total_shares"} <= set(
        recipe["required_documents"]
    )


def test_business_model_overlay_adds_evidence_without_changing_domain():
    recipe = build_research_recipe([
        _profile("advanced_manufacturing", charging="项目交付")
    ])

    assert recipe["adapters"] == ["advanced_manufacturing"]
    assert recipe["business_models"] == ["project_delivery"]
    assert "backlog_acceptance_and_collection" in recipe["required_documents"]
