"""Regression guards for the CI security and diagnostic contracts (no YAML dependency)."""
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_every_external_action_is_pinned_to_a_commit():
    for workflow in WORKFLOWS.glob("*.yml"):
        for action in re.findall(r"^\s+(?:- )?uses: (\S+)", workflow.read_text(), re.MULTILINE):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), (workflow, action)


def test_device_free_matrix_covers_advertised_pythons_and_retains_both_hosts():
    text = (WORKFLOWS / "ci.yml").read_text()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    versions = [c.rsplit(" :: ", 1)[1] for c in project["classifiers"]
                if c.startswith("Programming Language :: Python :: 3.")]
    for version in versions:
        assert f'python-version: "{version}"' in text
    assert "os: macos-latest" in text and "os: ubuntu-latest" in text
    assert 'GHOSTDECK_HW_TEST: ""' in text


def test_ci_keeps_diagnostics_and_bounds_each_job():
    text = (WORKFLOWS / "ci.yml").read_text()
    test_job, dco_job = text.split("  dco:\n")
    assert "timeout-minutes:" in test_job and "timeout-minutes:" in dco_job
    assert "-ra" in test_job and "--durations=" in test_job
    assert "--junitxml=test-results/pytest.xml" in test_job
    artifact = test_job.split("      - name: Upload pytest results\n")[1]
    assert "if: always()" in artifact
    assert "${{ matrix.os }}" in artifact and "${{ matrix.python-version }}" in artifact
    assert "path: test-results/pytest.xml" in artifact
    assert 'python3 -I - "$BASE" "$HEAD"' in dco_job
    embedded = dco_job.split("<<'PY'\n", 1)[1].rsplit("          PY\n", 1)[0]
    checker = "\n".join(line[10:] for line in embedded.splitlines()) + "\n"
    assert checker == (ROOT / "tools" / "check_dco.py").read_text()
    assert "< <(" not in dco_job


def test_arm_guard_prerequisites_are_confined_to_one_existing_matrix_leg():
    text = (WORKFLOWS / "ci.yml").read_text()
    prerequisites = text.split("      - name: ARM archive guard test prerequisites\n")[1].split("      - name: Device-free suite")[0]
    assert "matrix.os == 'ubuntu-latest' && matrix.python-version == '3.12'" in prerequisites
    assert "gcc-arm-linux-gnueabihf" in prerequisites
    assert "binutils-arm-linux-gnueabihf" in prerequisites
    assert "armv7-linux-gnueabihf-$tool" in prerequisites
    assert '"$GITHUB_PATH"' in prerequisites


def test_only_tag_release_publisher_has_contents_write():
    text = (WORKFLOWS / "agent.yml").read_text()
    build, publisher = text.split("  release:\n")
    assert "contents: read" in build and "contents: write" not in build
    assert text.count("contents: write") == 1
    assert "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/')" in publisher
    assert "needs: armv7" in publisher
    assert "actions/download-artifact@" in publisher
    assert "name: d200-color-agent" in publisher
    assert "files: dist/d200-color-agent" in publisher
    assert "fail_on_unmatched_files: true" in publisher
    assert "timeout-minutes:" in publisher


def test_source_checksum_is_verified_before_extraction():
    text = (WORKFLOWS / "agent.yml").read_text()
    assert re.search(r'echo "[0-9a-f]{64}  libjpeg-turbo-3\.0\.3\.tar\.gz" \| sha256sum --check --strict', text)
    assert text.index("sha256sum --check --strict") < text.index("tar xzf ")
    assert "| tar" not in text
