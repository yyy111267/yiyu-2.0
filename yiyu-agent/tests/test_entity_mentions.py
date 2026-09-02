from toolkit.entity import mention


def test_extract_candidates_recognizes_maotai_short_alias() -> None:
    mention._names_cache = None
    mention._fullname_set = None
    assert mention.extract_candidates("分析茅台") == ["茅台"]


def test_extract_candidates_keeps_maotai_full_name_single_target() -> None:
    mention._names_cache = None
    mention._fullname_set = None
    candidates = mention.extract_candidates("请分析贵州茅台")
    assert candidates[0] == "贵州茅台"
    assert "茅台" in candidates
