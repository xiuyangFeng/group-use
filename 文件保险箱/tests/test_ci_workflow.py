"""检查 GitHub Actions 工作流本身没写错（矩阵、测试、打包、产物上传）。"""

from __future__ import annotations

from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parent.parent
WORKFLOW = PROJECT / ".github" / "workflows" / "build.yml"


def _load() -> dict:
    # PyYAML 按 YAML 1.1 解析，`on:` 会变成布尔键 True，这里兼容两种
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_workflow_triggers_and_matrix():
    data = _load()
    triggers = data.get("on", data.get(True))
    assert triggers, "缺少 on 触发器"
    assert "push" in triggers and "workflow_dispatch" in triggers

    job = data["jobs"]["build"]
    include = job["strategy"]["matrix"]["include"]
    names = {(row["os"], row["arch"]) for row in include}
    assert ("macos-14", "arm64") in names
    assert ("windows-latest", "x64") in names


def test_workflow_runs_tests_selftest_and_build():
    job = _load()["jobs"]["build"]
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "pytest tests" in runs
    assert "gui_selftest.py" in runs
    assert "build_app.py" in runs

    upload = next(step for step in job["steps"] if "upload-artifact" in step.get("uses", ""))
    paths = upload["with"]["path"]
    assert "dist/*.zip" in paths
    assert "dist/*.dmg" in paths
    assert "SHA256SUMS.txt" in paths


def test_ci_test_count_documented_matches_reality():
    """README 里写的测试数量要和实际收集到的一致（防止改代码忘改文档）。"""
    import re
    import subprocess
    import sys

    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    claimed = set(int(x) for x in re.findall(r"(\d+) 个自动化测试", readme))
    assert claimed, "README 里找不到测试数量说明"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only"],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    m = re.search(r"(\d+) tests? collected", proc.stdout + proc.stderr)
    assert m, proc.stdout + proc.stderr
    actual = int(m.group(1))
    for n in claimed:
        assert n == actual, f"README 写 {n} 个，实际收集 {actual} 个"
