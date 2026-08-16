from __future__ import annotations

import io

import pytest
from rich.console import Console

from polymarket_doctor.checks import default_registry
from polymarket_doctor.cli import build_parser, main
from polymarket_doctor.core.check import Finding, Severity, Stage
from polymarket_doctor.core.runner import Outcome, RunReport
from polymarket_doctor.render.terminal import TerminalReport


def test_every_registered_check_resolves():
    # Catches a check added with a typo'd fact or a dependency cycle at import
    # time rather than when someone runs it.
    registry = default_registry()
    assert len(registry.resolve()) == len(registry)


def test_check_ids_are_stage_prefixed():
    # The id doubles as the `check` argument, so it should say where it belongs.
    prefixes = {
        Stage.ENVIRONMENT: "env.",
        Stage.IDENTITY: "identity.",
        Stage.AUTH: "auth.",
        Stage.FUNDING: "funding.",
        Stage.MARKET_LIMITS: "market.",
        Stage.ORDER_DRY_RUN: "order.",
        Stage.WEBSOCKET: "ws.",
        Stage.RFQ: "rfq.",
    }
    for check in default_registry():
        assert check.id.startswith(prefixes[check.stage]), check.id


def test_list_command_exits_clean(capsys):
    assert main(["list"]) == 0
    assert "env.reachable" in capsys.readouterr().out


def test_partial_credentials_are_rejected_before_any_request(capsys):
    # Usage error: exit 2 with a message on stderr, not a traceback and not
    # the exit-1 that means "integration broken".
    code = main(["onboard", "--address", "0x" + "1" * 40, "--api-key", "k", "--api-secret", "s"])
    assert code == 2
    assert "--api-passphrase" in capsys.readouterr().err


def test_secret_flag_help_steers_at_the_environment():
    parser = build_parser()
    help_text = parser.format_help() + _subcommand_help(parser, "onboard")
    assert "shell history" in help_text


def _subcommand_help(parser, name: str) -> str:
    action = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    return action.choices[name].format_help()


class TestTerminalRender:
    """Rendering shouldn't crash on any severity, and must show the fix."""

    def _report(self, *findings: Finding) -> RunReport:
        report = RunReport()
        checks = list(default_registry())
        for check, finding in zip(checks, findings, strict=False):
            report.outcomes.append(Outcome(check, finding, 1.0))
        return report

    @pytest.mark.parametrize("finding", [
        Finding.ok("a-passing-summary"),
        Finding.warn("a-warning-summary", detail="because", remedy="do this"),
        Finding.fail("a-failing-summary", detail="because", remedy="do that"),
        Finding(Severity.SKIP, "a-skipped-summary"),
    ])
    def test_renders_each_severity(self, finding):
        # Don't just assert it didn't crash: the summary must actually appear,
        # or a renderer that silently dropped a severity would ship green.
        console = Console(record=True, width=100, file=io.StringIO())
        TerminalReport(console).render(self._report(finding), host="https://example.test")
        assert finding.summary in console.export_text()

    def test_failure_output_carries_remedy_and_issue(self):
        from polymarket_doctor import issues

        console = Console(record=True, width=100, file=io.StringIO())
        TerminalReport(console).render(
            self._report(Finding.fail(
                "key bound to the wrong address",
                detail="mechanism",
                remedy="use the deposit wallet",
                issue=issues.SIGNER_IDENTITY_MISMATCH,
            )),
            host="https://example.test",
        )
        output = console.export_text()

        assert "use the deposit wallet" in output
        assert "py-clob-client-v2#70" in output
        # With every stage implemented, the not-a-green-light caveat is retired;
        # it only reappears if a stage is ever pulled back into PENDING_STAGES.
        assert "not a green light" not in output


def test_unknown_check_id_suggests_the_closest_real_one(capsys):
    exit_code = main(["check", "auth.key_identity", "--address", "0x" + "1" * 40, "--no-rpc"])
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "auth.key-identity" in err       # the suggestion
    assert "Traceback" not in err


def test_onboard_json_format_emits_a_parseable_document(capsys, monkeypatch):
    # Drive main() with a stub probe so it doesn't hit the network, then assert
    # stdout is the JSON contract, not Rich output.
    import json as _json

    from polymarket_doctor import cli
    from polymarket_doctor.core.runner import RunReport

    def fake_run(self, ctx, only=None):
        return RunReport()  # empty run: ok, exit 0

    monkeypatch.setattr(cli.Runner, "run", fake_run)
    code = cli.main(["onboard", "--address", "0x" + "1" * 40, "--no-rpc", "--format", "json"])
    out = capsys.readouterr().out

    doc = _json.loads(out)  # must parse
    assert doc["schema_version"] == "1.0"
    assert doc["exit_code"] == code == 0


def test_passing_check_with_an_issue_renders_a_dim_note():
    # A PASS can still carry a heads-up issue (a fine-tick market that works but
    # is bitten by #99 in prod). The note and its issue URL must render.
    from polymarket_doctor import issues
    from polymarket_doctor.core.check import Finding
    from polymarket_doctor.core.runner import Outcome, RunReport
    from polymarket_doctor.render.terminal import TerminalReport

    report = RunReport()
    check = next(iter(default_registry()))
    report.outcomes.append(Outcome(
        check, Finding.ok("works, but watch out", detail="a caveat",
                          issue=issues.FINE_TICK_REJECTED), 1.0))

    console = Console(record=True, width=100, file=io.StringIO())
    TerminalReport(console).render(report, host="https://example.test")
    out = console.export_text()
    assert "a caveat" in out
    assert "py-clob-client-v2#99" in out


def test_pending_stages_render_when_some_stage_is_unimplemented(monkeypatch):
    # All eight stages ship today, so the "not implemented" rendering is dead
    # unless a stage is pulled back. Simulate that to prove the path still works.
    from polymarket_doctor.core.check import Finding, Stage
    from polymarket_doctor.core.runner import Outcome, RunReport
    from polymarket_doctor.render import terminal

    monkeypatch.setattr(terminal, "PENDING_STAGES",
                        ((Stage.RFQ, "quote submission and last look"),))

    report = RunReport()
    check = next(iter(default_registry()))
    report.outcomes.append(Outcome(check, Finding.fail("boom"), 1.0))

    console = Console(record=True, width=100, file=io.StringIO())
    terminal.TerminalReport(console).render(report, host="https://example.test")
    out = console.export_text()
    assert "not implemented" in out
    assert "not a green light" in out  # the footer caveat returns with pending stages


def test_full_credential_flags_build_credentials(monkeypatch):
    # The accept path of _credentials_from (all three flags present).
    from polymarket_doctor import cli
    from polymarket_doctor.core.runner import RunReport

    monkeypatch.setattr(cli.Runner, "run", lambda self, ctx, only=None: RunReport())
    code = cli.main(["onboard", "--address", "0x" + "1" * 40, "--no-rpc",
                     "--api-key", "k", "--api-secret", "c2VjcmV0", "--api-passphrase", "p"])
    assert code == 0


def test_onboard_text_format_renders_through_main(monkeypatch, capsys):
    # The default text path through main() (not the JSON branch).
    import httpx

    from polymarket_doctor import cli
    from polymarket_doctor.net.http import HttpxProbe

    def handler(request):
        if "/version" in str(request.url):
            return httpx.Response(200, json={"version": 2})
        return httpx.Response(200, json={})

    original = HttpxProbe.__init__

    def patched(self, *a, **k):
        original(self, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli.HttpxProbe, "__init__", patched)
    cli.main(["onboard", "--address", "0x" + "1" * 40, "--no-rpc"])
    assert "environment" in capsys.readouterr().out


def test_verify_order_reads_stdin(monkeypatch, capsys):
    import io
    import json as _json

    from polymarket_doctor import cli

    # A minimal order missing fields -> stops at shape, but exercises stdin read.
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps({"maker": "0x1"})))
    code = cli.main(["verify-order"])
    assert code == 1  # rejected (incomplete order), stdin was read


def test_unreadable_file_that_is_a_directory_exits_2(tmp_path):
    code = main(["verify-order", "--file", str(tmp_path)])  # a dir, not a file
    assert code == 2


def test_top_level_guard_maps_interrupt_and_errors(monkeypatch):
    from polymarket_doctor import cli

    monkeypatch.setattr(cli, "_run", lambda argv: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.main(["list"]) == 130

    monkeypatch.setattr(cli, "_run", lambda argv: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cli.main(["list"]) == 2


def test_broken_pipe_exits_zero_without_erroring(monkeypatch):
    # The guard closes stdout on a broken pipe; give it a throwaway so it
    # doesn't close pytest's captured stream.
    import io

    from polymarket_doctor import cli

    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr(cli, "_run", lambda argv: (_ for _ in ()).throw(BrokenPipeError()))
    assert cli.main(["list"]) == 0


def test_module_entrypoints_run(monkeypatch):
    # __main__.py and cli.py's `if __name__ == "__main__"` guard. runpy executes
    # them in-process (so coverage counts the lines); both call sys.exit(main()).
    import runpy
    import sys

    for target in ("polymarket_doctor", "polymarket_doctor.cli"):
        monkeypatch.setattr("sys.argv", [target, "list"])
        # Drop it from the module cache so runpy executes it fresh without the
        # "already imported" RuntimeWarning.
        sys.modules.pop(target, None)
        with pytest.raises(SystemExit) as exit_:
            runpy.run_module(target, run_name="__main__")
        assert exit_.value.code == 0
