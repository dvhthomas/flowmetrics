"""Behavioural spec for the named-signal constants module.

Every source event (GitHub label-added, PR merged, Jira changelog
transition, etc.) carries a constant from `signals.py` so the
audit trail is in the data itself, not in pattern-matching code.

Inspired by gh-velocity's `model.Signal*` constants.

The test asserts:
1. Each named constant exists and is a non-empty string.
2. Every constant is unique (no accidental aliasing).
3. The set covers every source event the spec calls out.
"""

from __future__ import annotations

from flowmetrics import signals

REQUIRED_GITHUB_SIGNALS = {
    "SIGNAL_GITHUB_ISSUE_CREATED",
    "SIGNAL_GITHUB_ISSUE_CLOSED",
    "SIGNAL_GITHUB_LABEL_ADDED",
    "SIGNAL_GITHUB_LABEL_REMOVED",
    "SIGNAL_GITHUB_PR_CREATED",
    "SIGNAL_GITHUB_PR_READY_FOR_REVIEW",
    "SIGNAL_GITHUB_PR_REVIEW_CHANGES_REQUESTED",
    "SIGNAL_GITHUB_PR_REVIEW_APPROVED",
    "SIGNAL_GITHUB_PR_MERGED",
    "SIGNAL_GITHUB_PR_CLOSES_ISSUE",
}

REQUIRED_JIRA_SIGNALS = {
    "SIGNAL_JIRA_ISSUE_CREATED",
    "SIGNAL_JIRA_STATUS_CHANGED",
    "SIGNAL_JIRA_RESOLVED",
}


class TestSignalConstants:
    def test_required_github_signals_exist(self):
        for name in REQUIRED_GITHUB_SIGNALS:
            assert hasattr(signals, name), f"signals.py missing {name}"
            value = getattr(signals, name)
            assert isinstance(value, str)
            assert value, f"{name} must be a non-empty string"

    def test_required_jira_signals_exist(self):
        for name in REQUIRED_JIRA_SIGNALS:
            assert hasattr(signals, name), f"signals.py missing {name}"
            value = getattr(signals, name)
            assert isinstance(value, str)
            assert value

    def test_all_constants_are_unique(self):
        """No accidental aliases — two constants pointing to the
        same string would break audit-trail interpretation."""
        names = REQUIRED_GITHUB_SIGNALS | REQUIRED_JIRA_SIGNALS
        values = [getattr(signals, n) for n in names]
        assert len(values) == len(set(values)), (
            f"duplicate signal values: {values}"
        )

    def test_github_signals_share_a_namespace_prefix(self):
        """Convention: GitHub signals carry a `github-` prefix in
        their string value so the audit trail self-documents."""
        for name in REQUIRED_GITHUB_SIGNALS:
            value = getattr(signals, name)
            assert value.startswith("github-"), (
                f"{name}={value!r} should start with 'github-'"
            )

    def test_jira_signals_share_a_namespace_prefix(self):
        for name in REQUIRED_JIRA_SIGNALS:
            value = getattr(signals, name)
            assert value.startswith("jira-"), (
                f"{name}={value!r} should start with 'jira-'"
            )
