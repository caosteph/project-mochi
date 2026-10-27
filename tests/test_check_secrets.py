"""scripts/check_secrets.py — the pre-push / CI guard for rule 5 (secrets never get committed).

The fakes below are assembled by concatenation so this file never matches the scanner's own
patterns (it runs over the whole repo in CI). The end-to-end tests build a real throwaway git
repo, because the case that matters most — a key added and then deleted — only shows up in
history, never in the final tree.
"""

import subprocess

import pytest

from scripts import check_secrets as cs

FAKES = {
    "Telegram bot token": "8736433076:AA" + "x" * 33,
    "Groq API key": "gsk_" + "A1b2" * 12,
    "Tavily API key": "tvly-" + "dev-" + "q" * 24,
    "OpenAI/OpenRouter-style key": "sk-or-v1-" + "f" * 48,
    "Google OAuth client secret": "GOCSPX-" + "Z" * 28,
    "Google OAuth refresh token": "1//0" + "g" * 40,
    "GitHub token": "ghp_" + "h" * 36,
    "Private key": "-----BEGIN RSA " + "PRIVATE KEY-----",
}


@pytest.mark.parametrize(("rule", "value"), FAKES.items())
def test_each_credential_format_is_caught(rule, value):
    assert rule in cs.scan_line(f'HOSTED_API_KEY = "{value}"')


@pytest.mark.parametrize(("rule", "value"), [
    ("Telegram bot token", "123456789:AA" + "x" * 32 + "-"),   # ~1 in 64 real tokens end in '-'
    ("Google API key", "AIza" + "k" * 34 + "-"),
])
def test_keys_ending_in_a_dash_are_caught(rule, value):
    """Regression: a trailing \\b needs a word char, so these slipped through (found in review)."""
    assert rule in cs.scan_line(f"KEY={value}")


@pytest.mark.parametrize("benign", [
    "TELEGRAM_BOT_TOKEN=",                                   # .env.example placeholder
    'os.environ.setdefault("TELEGRAM_BOT_TOKEN", "verify_placeholder")',
    "skip-the-line and sk-short",                            # 'sk-' prose, too short to be a key
    "tracking number 1Z999AA10123456784",
    "commit 5631e1e5de0c459a2f1fb4a485346bfa9634d48b",       # a sha is not a token
])
def test_ordinary_lines_pass(benign):
    assert cs.scan_line(benign) == []


def test_allow_mark_exempts_a_deliberate_fixture():
    assert cs.scan_line(FAKES["Groq API key"] + "  # " + cs.ALLOW_MARK) == []


@pytest.mark.parametrize(("path", "bad"), [
    (".env", True), ("app/.env", True), ("data/google_token.json", True),
    (".env.local", True), (".env.production", True), ("app/.env.bak", True),
    ("data/profile.json", True), ("secrets/client_secret_123.json", True), ("id_rsa.pem", True),
    (".env.example", False), ("data/.gitkeep", False), ("app/config.py", False),
    ("docs/token-budget.md", False),
])
def test_forbidden_paths(path, bad):
    assert cs.forbidden_path(path) is bad


def test_pre_push_ranges():
    zero = "0" * 40
    stdin = "\n".join([
        f"refs/heads/a {'1' * 40} refs/heads/a {'2' * 40}",  # update → remote..local
        f"refs/heads/b {'3' * 40} refs/heads/b {zero}",       # new branch → not on any remote
        f"(delete) {zero} refs/heads/c {'4' * 40}",           # deletion → nothing to scan
    ])
    assert cs.pre_push_ranges(stdin, have_commit=lambda _sha: True) == [
        [f"{'2' * 40}..{'1' * 40}"],
        ["3" * 40, "--not", "--remotes"],
    ]


def test_an_unfetched_remote_sha_falls_back_instead_of_crashing():
    """Regression: a force-push after the remote moved handed git log a sha we don't have, and the
    hook died with a traceback. Now it scans everything not on a known remote."""
    stdin = f"refs/heads/a {'1' * 40} refs/heads/a {'9' * 40}"
    assert cs.pre_push_ranges(stdin, have_commit=lambda _sha: False) == [["1" * 40, "--not", "--remotes"]]


# --- end to end against a real git repo -------------------------------------------


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("hello\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _scan(repo, *rev):
    """Through the scanner's own git invocation, so its merge/rename flags are what's tested."""
    return cs.scan_log(cs._git_log(list(rev), cwd=str(repo)))


def test_clean_history_passes(repo):
    assert _scan(repo, "--all") == []


def test_a_key_added_then_deleted_is_still_caught(repo):
    """The final tree is clean, but the key is in history — exactly the unrecoverable case."""
    cfg = repo / "config.py"
    cfg.write_text("x = 1\n" + f'KEY = "{FAKES["Groq API key"]}"\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add key")
    cfg.write_text("x = 1\n")
    _git(repo, "commit", "-qam", "remove key")
    findings = _scan(repo, "--all")
    assert [(f.path, f.line, f.rule) for f in findings] == [("config.py", 2, "Groq API key")]


def test_a_force_added_env_file_is_caught_whatever_it_contains(repo):
    (repo / ".env").write_text("NOTHING_SECRET_LOOKING=1\n")
    _git(repo, "add", "-f", ".env")
    _git(repo, "commit", "-qm", "oops")
    assert [(f.path, f.rule) for f in _scan(repo, "--all")] == [(".env", "file must never be committed")]


def test_only_the_pushed_range_is_scanned(repo):
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.py").write_text("print('fine')\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fine")
    assert _scan(repo, f"{base}..HEAD") == []


def test_a_key_introduced_in_a_merge_commit_is_caught(repo):
    """Regression: plain `git log -p` prints no patch for merges, so a key typed while resolving a
    conflict was invisible to the scan."""
    _git(repo, "checkout", "-qb", "side")
    (repo / "README.md").write_text("side\n")
    _git(repo, "commit", "-qam", "side")
    _git(repo, "checkout", "-q", "-")
    (repo / "README.md").write_text("main\n")
    _git(repo, "commit", "-qam", "main")
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "side"], capture_output=True)  # conflicts
    (repo / "README.md").write_text(f'resolved\nKEY = "{FAKES["GitHub token"]}"\n')
    _git(repo, "commit", "-qam", "merge side")
    assert ("README.md", "GitHub token") in [(f.path, f.rule) for f in _scan(repo, "--all")]


def test_renaming_a_file_into_data_is_caught(repo):
    (repo / "notes.json").write_text("{}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "notes")
    (repo / "data").mkdir()
    _git(repo, "mv", "notes.json", "data/creds.json")
    _git(repo, "commit", "-qm", "move")
    assert ("data/creds.json", "file must never be committed") in [(f.path, f.rule) for f in _scan(repo, "--all")]


def test_a_binary_secret_file_is_caught(repo):
    (repo / "id_rsa.pem").write_bytes(b"\x00\x01binary\x00")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "key")
    assert [(f.path, f.rule) for f in _scan(repo, "--all")] == [("id_rsa.pem", "file must never be committed")]
