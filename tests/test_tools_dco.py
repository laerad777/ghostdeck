"""DCO must fail closed and validate trailers, not matching text in commit bodies."""
import importlib.util
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_dco.py"
SPEC = importlib.util.spec_from_file_location("check_dco", SCRIPT)
dco = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dco)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    dco.git("config", "user.name", "Test Contributor")
    dco.git("config", "user.email", "contributor@example.com")
    dco.git("config", "commit.gpgsign", "false")
    dco.git("commit", "--allow-empty", "-m", "Base")
    return dco.git("rev-parse", "HEAD").strip()


def commit(message):
    dco.git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "--cleanup=verbatim", "-m", message)
    return dco.git("rev-parse", "HEAD").strip()


@pytest.mark.parametrize("message", [
    "Change\n\nSigned-off-by: Test Contributor <contributor@example.com>",
    "Change\n\nBody text\n\nReviewed-by: Reviewer <reviewer@example.com>\nSigned-off-by: Test Contributor <contributor@example.com>\n",
    "Change\n\nSigned-off-by: First Person <first@example.com>\nSigned-off-by: Second Person <second@example.com>",
])
def test_real_signoff_trailers_pass(repo, message):
    assert dco.main([repo, commit(message)]) == 0


@pytest.mark.parametrize("message", [
    "Unsigned change",
    "Signed-off-by: Test Contributor <contributor@example.com>",  # subject only
    "Change\n\nSigned-off-by: Test Contributor <contributor@example.com>\n\nMore body text.",
    "Change\n\nExample Signed-off-by: Test Contributor <contributor@example.com>",
    "Change\n\nSigned-off-by: ",
    "Change\n\nSigned-off-by: <contributor@example.com>",
    "Change\n\nSigned-off-by: Test Contributor <>",
    "Change\n\nSigned-off-by: Test Contributor <not-an-email>",
    "Change\n\nSigned-off-by: Test Contributor <user@example.com> trailing junk",
])
def test_missing_malformed_and_body_only_signoffs_fail(repo, message):
    assert dco.main([repo, commit(message)]) == 1


def test_every_non_merge_commit_is_checked(repo):
    commit("Unsigned first change")
    head = commit("Signed second change\n\nSigned-off-by: Test Contributor <contributor@example.com>")
    assert dco.main([repo, head]) == 1


def test_empty_valid_range_is_reported(repo, capsys):
    assert dco.main([repo, repo]) == 0
    assert "checked 0" in capsys.readouterr().out


@pytest.mark.parametrize("base,head", [("missing", "HEAD"), ("HEAD", "missing"), ("--all", "HEAD")])
def test_invalid_range_fails_closed(repo, base, head):
    assert dco.main([base, head]) == 2


@pytest.mark.parametrize("command", ["rev-list", "log", "interpret-trailers"])
def test_git_failures_are_never_a_pass(repo, monkeypatch, command):
    head = commit("Change\n\nSigned-off-by: Test Contributor <contributor@example.com>")
    real_git = dco.git
    def failing_git(*args, **kwargs):
        if args[0] == command:
            raise subprocess.CalledProcessError(128, ["git", *args], stderr="injected failure")
        return real_git(*args, **kwargs)
    monkeypatch.setattr(dco, "git", failing_git)
    assert dco.main([repo, head]) == 2


def test_missing_git_and_bad_arguments_fail_closed(monkeypatch):
    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git not found")
    monkeypatch.setattr(dco, "git", missing_git)
    assert dco.main(["BASE", "HEAD"]) == 2
    assert dco.main([]) == 2


def test_merge_commits_are_exempt_but_branch_commits_are_checked(repo):
    main_branch = dco.git("branch", "--show-current").strip()
    dco.git("checkout", "-b", "feature")
    commit("Signed feature\n\nSigned-off-by: Test Contributor <contributor@example.com>")
    dco.git("checkout", main_branch)
    commit("Signed main\n\nSigned-off-by: Test Contributor <contributor@example.com>")
    dco.git("merge", "--no-ff", "feature", "-m", "Unsigned merge commit")
    assert dco.main([repo, "HEAD"]) == 0
