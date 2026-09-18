from datetime import datetime, timezone
from pathlib import Path

import evaluation.e2e.scripts.verify_report as verifier


def test_verify_report_rejects_stale_or_mismatched_report(tmp_path, monkeypatch):
    cases_dir = tmp_path / "cases"
    prompts_dir = tmp_path / "prompts"
    cases_dir.mkdir()
    prompts_dir.mkdir()
    (cases_dir / "case.yaml").write_text("cases: []\n", encoding="utf-8")
    (prompts_dir / "prompt.md").write_text("current", encoding="utf-8")
    monkeypatch.setattr(verifier, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verifier, "load_cases", lambda _: [])
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"stdout": "current-sha\n"})(),
    )

    report = tmp_path / "report.json"
    report.write_text(
        """{
          "run": {
            "generated_at": "2020-01-01T00:00:00+00:00",
            "git_sha": "old-sha",
            "execution_mode": "offline",
            "case_count": 99,
            "case_fingerprint": "old",
            "prompt_fingerprint": "old",
            "source_fingerprint": "old"
          },
          "summary": {"total": 1, "failed": 1, "skipped": 1},
          "cases": [{}]
        }""",
        encoding="utf-8",
    )

    errors = verifier.verify_report(report, cases_dir)

    assert any("报告已过期" in error for error in errors)
    assert any("git_sha 不匹配" in error for error in errors)
    assert any("execution_mode 不匹配" in error for error in errors)
    assert any("报告含失败用例" in error for error in errors)


def test_verify_report_accepts_matching_fresh_report(tmp_path, monkeypatch):
    cases_dir = tmp_path / "cases"
    prompts_dir = tmp_path / "prompts"
    cases_dir.mkdir()
    prompts_dir.mkdir()
    case_file = cases_dir / "case.yaml"
    prompt_file = prompts_dir / "prompt.md"
    case_file.write_text("cases: []\n", encoding="utf-8")
    prompt_file.write_text("current", encoding="utf-8")
    monkeypatch.setattr(verifier, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verifier, "load_cases", lambda _: [])
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"stdout": "current-sha\n"})(),
    )

    report = tmp_path / "report.json"
    report.write_text(
        __import__("json").dumps({
            "run": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "git_sha": "current-sha",
                "execution_mode": "live",
                "case_count": 0,
                "case_fingerprint": verifier._fingerprint([case_file]),
                "prompt_fingerprint": verifier._fingerprint([prompt_file]),
                "source_fingerprint": verifier._fingerprint(
                    verifier._source_files(tmp_path)
                ),
            },
            "summary": {"total": 0, "failed": 0, "skipped": 0},
            "cases": [],
        }),
        encoding="utf-8",
    )

    assert verifier.verify_report(report, cases_dir) == []
