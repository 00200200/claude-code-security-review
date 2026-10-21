"""Exercise the composite action's shell steps without calling external services."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ACTION = yaml.safe_load((Path(__file__).resolve().parents[1] / "action.yml").read_text())


def step_script(name, **expressions):
    step = next(step for step in ACTION["runs"]["steps"] if step["name"] == name)
    script = step["run"]
    for expression, value in expressions.items():
        script = script.replace("${{ " + expression + " }}", value)
    assert "${{" not in script
    return script


def executable(path, body):
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)


def run_shell(script, workspace, env):
    return subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.fixture
def scan(tmp_path):
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    action = tmp_path / "action"
    (action / "claudecode").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("cat", "cp", "head", "ls", "rm", "wc"):
        (bin_dir / command).symlink_to(shutil.which(command))
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("action shell tests require jq")
    (bin_dir / "jq").symlink_to(jq)
    executable(bin_dir / "claude", "echo 'mock Claude CLI'")
    executable(
        bin_dir / "python",
        'if [ "$1" = "--version" ]; then echo "mock Python"; exit 0; fi\n'
        'echo "scan diagnostics" >&2\n'
        'if [ "$MISSING_RESULT" = true ]; then\n'
        '  rm claudecode/claudecode-results.json\n'
        'else\n'
        '  printf "%s" "$SCAN_RESULT"\n'
        'fi\n'
        'exit "$SCAN_EXIT_CODE"',
    )
    output = tmp_path / "outputs"
    output.touch()

    def run(result, *, jq_body=None, missing_jq=False, exit_code=0):
        if missing_jq or jq_body is not None:
            (bin_dir / "jq").unlink()
        if jq_body is not None:
            executable(bin_dir / "jq", jq_body)
        env = {
            "PATH": str(bin_dir),
            "HOME": str(tmp_path),
            "ACTION_PATH": str(action),
            "GITHUB_WORKSPACE": str(workspace),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REPOSITORY": "example/repo",
            "PR_NUMBER": "1",
            "ANTHROPIC_API_KEY": "test-only-unused-key",
            "CLAUDECODE_TIMEOUT": "1",
            "SCAN_RESULT": result or "",
            "MISSING_RESULT": "true" if result is None else "false",
            "SCAN_EXIT_CODE": str(exit_code),
            "REAL_JQ": jq,
        }
        script = step_script(
            "Run ClaudeCode scan",
            **{"github.event_name": "pull_request", "github.workspace": str(workspace)},
        )
        completed = run_shell(script, workspace, env)
        return completed, workspace, output.read_text()

    return run


@pytest.mark.parametrize(
    "result",
    [
        "", "x", "{invalid", '{"error":"scan failed"}', None,
        " \n ", "null", "[]", '"text"', "{}\n{}",
        '{"findings":{}}', '{"findings":false}', '{"findings":"broken"}',
    ],
)
def test_scan_failures_preserve_artifacts_without_clean_output(scan, result):
    completed, workspace, outputs = scan(result)
    assert completed.returncode != 0
    assert "::error::" in completed.stdout
    assert "findings_count=" not in outputs
    assert (workspace / "claudecode-error.log").read_text() == "scan diagnostics\n"
    if result is not None:
        assert (workspace / "claudecode-results.json").read_text() == result


@pytest.mark.parametrize(
    "jq_body",
    [
        None,
        "exit 2",
        'if [ "$1" = "-e" ] && [ "$2" = ".error" ]; then exit 2; fi\nexec "$REAL_JQ" "$@"',
        'if [ "$1" = "-r" ]; then exit 2; fi\nexec "$REAL_JQ" "$@"',
        'if [ "$1" = ".findings // []" ]; then exit 2; fi\nexec "$REAL_JQ" "$@"',
    ],
    ids=["missing", "broken", "error-check-fails", "count-fails", "extraction-fails"],
)
def test_jq_failures_preserve_artifacts_without_clean_output(scan, jq_body):
    result = '{"findings":[]}'
    completed, workspace, outputs = scan(result, jq_body=jq_body, missing_jq=jq_body is None)
    assert completed.returncode != 0
    assert "::error::" in completed.stdout
    assert "findings_count=" not in outputs
    assert (workspace / "claudecode-results.json").read_text() == result
    assert (workspace / "claudecode-error.log").read_text() == "scan diagnostics\n"


@pytest.mark.parametrize(
    "result, expected, exit_code",
    [
        ({"findings": []}, [], 0),
        ({"findings": None}, [], 0),
        ({}, [], 0),
        ({"findings": [{"severity": "HIGH"}]}, [{"severity": "HIGH"}], 0),
        ({"findings": [{"severity": "HIGH"}]}, [{"severity": "HIGH"}], 1),
    ],
)
def test_valid_scan_results_are_preserved(scan, result, expected, exit_code):
    completed, workspace, outputs = scan(json.dumps(result), exit_code=exit_code)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert outputs.count("findings_count=") == 1
    assert f"findings_count={len(expected)}\n" in outputs
    assert json.loads((workspace / "findings.json").read_text()) == expected
    assert json.loads((workspace / "claudecode-results.json").read_text()) == result


@pytest.mark.parametrize("tool, step", [("gh", "Install GitHub CLI"), ("jq", "Install dependencies")])
@pytest.mark.parametrize(
    "scenario",
    ["preinstalled", "no-installer", "sudo", "no-sudo", "update-fails", "install-fails"],
)
def test_cli_installation(tmp_path, tool, step, scenario):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "install.log"
    if scenario == "preinstalled":
        executable(bin_dir / tool, "exit 0")
    if scenario != "no-installer":
        executable(
            bin_dir / "apt-get",
            'echo "$*" >> "$INSTALL_LOG"\n'
            'if [ "$SCENARIO" = update-fails ] && [ "$1" = update ]; then exit 1; fi\n'
            'if [ "$1" = install ]; then\n'
            '  if [ "$SCENARIO" = install-fails ]; then exit 1; fi\n'
            '  printf "#!/bin/bash\\nexit 0\\n" > "$TEST_BIN/$TOOL"\n'
            '  /bin/chmod +x "$TEST_BIN/$TOOL"\n'
            'fi',
        )
    if scenario in ("preinstalled", "sudo", "update-fails", "install-fails"):
        executable(bin_dir / "sudo", 'echo "sudo $*" >> "$INSTALL_LOG"\nexec "$@"')
    script = step_script(step, **{"steps.claudecode-check.outputs.enable_claudecode": "false"})
    completed = run_shell(
        script,
        tmp_path,
        {
            "PATH": str(bin_dir),
            "HOME": str(tmp_path),
            "TOOL": tool,
            "TEST_BIN": str(bin_dir),
            "INSTALL_LOG": str(log),
            "SCENARIO": scenario,
        },
    )
    if scenario in ("no-installer", "update-fails", "install-fails"):
        assert completed.returncode != 0
        assert "::error::" in completed.stdout
    else:
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert (bin_dir / tool).exists()
    if scenario == "preinstalled":
        assert not log.exists()
