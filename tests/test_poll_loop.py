import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
POLL_LOOP = PROJECT / ".github" / "scripts" / "poll_loop.sh"
BASH = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"


class PollLoopTests(unittest.TestCase):
    def run_loop(self, *, dirty_sequence="1", push_sequence="0", commit_rc="0", two_iterations=False):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            (temp / "state.json").write_text("{}", encoding="utf-8")
            self.write_fake(fake_bin / "date", """#!/usr/bin/env bash
n=$(cat "$FAKE_STATE/date-count" 2>/dev/null || echo 0)
echo $((n + 1)) > "$FAKE_STATE/date-count"
if [ "${TWO_ITERATIONS:-0}" = 1 ]; then
  [ "$n" -le 2 ] && { echo 0; exit; }
  [ "$n" -le 6 ] && { echo 1; exit; }
  echo 200
else
  [ "$n" -le 2 ] && echo 0 || echo 2
fi
""")
            self.write_fake(fake_bin / "python", "#!/usr/bin/env bash\nexit 0\n")
            self.write_fake(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
            self.write_fake(fake_bin / "git", """#!/usr/bin/env bash
case "$1" in
  config|add|fetch|rebase) exit 0 ;;
  commit)
    n=$(cat "$FAKE_STATE/commit-count" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$FAKE_STATE/commit-count"
    [ "${GIT_COMMIT_RC:-0}" = 0 ] && touch "$FAKE_STATE/committed"
    exit "${GIT_COMMIT_RC:-0}"
    ;;
  push)
    n=$(cat "$FAKE_STATE/push-count" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$FAKE_STATE/push-count"
    value=$(echo "$PUSH_SEQUENCE" | cut -d, -f$((n + 1)))
    [ -n "$value" ] || value=1
    exit "$value"
    ;;
  diff)
    if [ "$2" = "--cached" ]; then
      [ -f "$FAKE_STATE/committed" ] && exit 0 || exit 1
    fi
    n=$(cat "$FAKE_STATE/diff-count" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$FAKE_STATE/diff-count"
    value=$(echo "$DIRTY_SEQUENCE" | cut -d, -f$((n + 1)))
    [ -n "$value" ] || value=0
    exit "$value"
    ;;
esac
exit 0
""")
            fake_bin_bash = self.bash_path(fake_bin)
            temp_bash = self.bash_path(temp)
            poll_loop_bash = self.bash_path(POLL_LOOP)
            env = os.environ | {
                # Start Bash with its inherited Windows PATH, then prepend a
                # POSIX fake directory after startup has converted paths.
                "FAKE_BIN": fake_bin_bash, "POLL_SCRIPT": poll_loop_bash,
                "FAKE_STATE": temp_bash, "DIRTY_SEQUENCE": dirty_sequence,
                "PUSH_SEQUENCE": push_sequence, "GIT_COMMIT_RC": commit_rc,
                "LOOP_SECONDS": "100" if two_iterations else "1",
                "POLL_INTERVAL": "1", "TWO_ITERATIONS": "1" if two_iterations else "0",
            }
            result = subprocess.run(
                [BASH, "-c", 'export PATH="$FAKE_BIN:$PATH"; exec bash "$POLL_SCRIPT"'],
                cwd=temp, env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False, timeout=15,
            )
            push_count = temp / "push-count"
            commit_count = temp / "commit-count"
            result.fake_pushes = int(push_count.read_text() if push_count.exists() else "0")
            result.fake_commits = int(commit_count.read_text() if commit_count.exists() else "0")
            return result

    @staticmethod
    def write_fake(path, body):
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    @staticmethod
    def bash_path(path):
        path = str(path)
        if os.name == "nt" and len(path) > 2 and path[1] == ":":
            return "/" + path[0].lower() + path[2:].replace("\\", "/")
        return path

    def test_successful_state_push_leaves_loop_green(self):
        result = self.run_loop()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("loop finished: 1 iterations", result.stdout)
        self.assertIn("1 state commits", result.stdout)
        self.assertEqual(result.fake_pushes, 1)
        self.assertEqual(result.fake_commits, 1)

    def test_commit_failure_makes_loop_fail(self):
        result = self.run_loop(commit_rc="1")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("could not commit state.json", result.stdout)
        self.assertIn("loop finished: 1 iterations", result.stdout)
        self.assertEqual(result.fake_pushes, 0)

    def test_failed_push_is_retried_after_pending_commit(self):
        # The first poll creates a commit and loses all four push attempts.
        # The next poll has no changed state but retries the pending commit.
        result = self.run_loop(
            dirty_sequence="1,0", push_sequence="1,1,1,1,0", two_iterations=True
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("push attempt 4 failed", result.stdout)
        self.assertIn("loop finished: 2 iterations", result.stdout)
        self.assertIn("1 state commits", result.stdout)
        self.assertEqual(result.fake_pushes, 5)
        self.assertEqual(result.fake_commits, 1)


if __name__ == "__main__":
    unittest.main()
