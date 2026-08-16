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
