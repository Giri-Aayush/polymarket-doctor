from __future__ import annotations

from datetime import date

from polymarket_doctor import issues


def test_open_issues_report_an_age():
    summary = issues.SIGNER_IDENTITY_MISMATCH.summarize(date(2026, 8, 15))
    assert "open 88d" in summary
    assert "44 affected" in summary


def test_closed_issues_report_the_date_not_an_age():
    # Rendering a resolved thread as "open 79d" misrepresents it, and the
    # resolution is the whole reason we cite this one.
    summary = issues.SIGNATURE_TYPE_MISMATCH.summarize(date(2026, 8, 15))
    assert "closed 2026-06-02" in summary
    assert "open" not in summary


def test_every_registered_issue_has_a_reachable_url():
    for issue in issues.ALL:
        assert issue.url.startswith("https://github.com/Polymarket/")
        assert str(issue.number) in issue.url
