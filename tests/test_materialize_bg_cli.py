"""`flow materialize --bg --at HH:MM` CLI plumbing.

Pins the flag combinatorics + that the right `bg` dispatcher
function fires with the right arguments. Real launchctl is not
invoked — we monkeypatch the `flowmetrics.bg` module's two
materialize verbs and inspect the calls.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from flowmetrics.cli import cli


def _patch_bg(monkeypatch):
    """Replace the dispatcher's materialize verbs with spies."""
    import flowmetrics.bg as bg
    install_calls: list[dict] = []
    stop_calls: list[None] = []

    def fake_install(**kwargs):
        install_calls.append(kwargs)
        return Path("/fake/com.flowmetrics.materialize.plist")

    def fake_stop():
        stop_calls.append(None)

    monkeypatch.setattr(bg, "install_materialize_schedule", fake_install)
    monkeypatch.setattr(bg, "stop_materialize_schedule", fake_stop)
    return install_calls, stop_calls


class TestArgValidation:
    def test_bg_without_at_or_stop_is_an_error(self, tmp_path, monkeypatch):
        _patch_bg(monkeypatch)
        res = CliRunner().invoke(cli, [
            "materialize", "--all", "--bg",
            "--workflows-dir", str(tmp_path / "contracts"),
            "--data-dir", str(tmp_path / "data"),
        ])
        assert res.exit_code != 0
        # Message points at the missing flag, not a traceback.
        msg = res.output.lower()
        assert "--at" in msg or "schedule" in msg

    def test_at_format_must_be_hh_mm(self, tmp_path, monkeypatch):
        _patch_bg(monkeypatch)
        res = CliRunner().invoke(cli, [
            "materialize", "--all", "--bg", "--at", "6am",
            "--workflows-dir", str(tmp_path / "contracts"),
            "--data-dir", str(tmp_path / "data"),
        ])
        assert res.exit_code != 0
        assert "hh:mm" in res.output.lower() or "format" in res.output.lower()

    def test_stop_requires_bg(self, tmp_path, monkeypatch):
        _patch_bg(monkeypatch)
        res = CliRunner().invoke(cli, [
            "materialize", "--stop", "--all",
            "--workflows-dir", str(tmp_path / "contracts"),
            "--data-dir", str(tmp_path / "data"),
        ])
        assert res.exit_code != 0
        assert "--stop" in res.output and "--bg" in res.output

    def test_neither_name_nor_all_with_bg_is_an_error(
        self, tmp_path, monkeypatch,
    ):
        """Same NAME-or-`--all` requirement applies whether we run
        materialize now or schedule it."""
        _patch_bg(monkeypatch)
        res = CliRunner().invoke(cli, [
            "materialize", "--bg", "--at", "06:00",
            "--workflows-dir", str(tmp_path / "contracts"),
            "--data-dir", str(tmp_path / "data"),
        ])
        assert res.exit_code != 0
        msg = res.output.lower()
        assert "--all" in msg or "name" in msg


class TestInstallSchedule:
    def test_bg_at_installs_with_args_passed_through(
        self, tmp_path, monkeypatch,
    ):
        install_calls, _ = _patch_bg(monkeypatch)
        wf = tmp_path / "contracts"
        data = tmp_path / "data"

        res = CliRunner().invoke(cli, [
            "materialize", "--all", "--bg", "--at", "06:00",
            "--workflows-dir", str(wf),
            "--data-dir", str(data),
        ])
        assert res.exit_code == 0, res.output
        assert len(install_calls) == 1
        kw = install_calls[0]
        assert kw["hour"] == 6
        assert kw["minute"] == 0
        # The materialize_args carried into the schedule must include
        # --all + the resolved paths. flow_bin is whatever
        # shutil.which finds; tests don't pin that.
        args = kw["materialize_args"]
        assert "--all" in args
        # Paths in the schedule must be absolute (launchd doesn't
        # inherit a CWD).
        wf_idx = args.index("--workflows-dir")
        assert Path(args[wf_idx + 1]).is_absolute()
        data_idx = args.index("--data-dir")
        assert Path(args[data_idx + 1]).is_absolute()

    def test_bg_at_bakes_absolute_cache_dir_into_args(
        self, tmp_path, monkeypatch,
    ):
        """launchd fires the scheduled job from CWD=`/` (sealed,
        read-only). A relative cache_dir default resolves to `/.cache/`
        and the job dies with `OSError [Errno 30]`. The install path
        must therefore bake an absolute --cache-dir into the plist
        args — never trust the CLI default to do the right thing once
        launchd is the caller."""
        install_calls, _ = _patch_bg(monkeypatch)
        data = tmp_path / "data"

        res = CliRunner().invoke(cli, [
            "materialize", "--all", "--bg", "--at", "06:00",
            "--workflows-dir", str(tmp_path / "contracts"),
            "--data-dir", str(data),
        ])
        assert res.exit_code == 0, res.output
        args = install_calls[0]["materialize_args"]
        assert "--cache-dir" in args, (
            f"--cache-dir missing from scheduled args: {args}"
        )
        cache_idx = args.index("--cache-dir")
        cache_val = Path(args[cache_idx + 1])
        assert cache_val.is_absolute(), (
            f"scheduled --cache-dir must be absolute, got {cache_val}"
        )

    def test_bg_at_default_cache_dir_lives_under_data_dir(
        self, tmp_path, monkeypatch,
    ):
        """When the operator doesn't pass --cache-dir, the scheduled
        job inherits a cache path under --data-dir. Co-locating means
        backup, cleanup, and 'what is flowmetrics keeping on disk'
        all live in one tree, and the launchd CWD problem can never
        bite again."""
        install_calls, _ = _patch_bg(monkeypatch)
        data = tmp_path / "data"

        res = CliRunner().invoke(cli, [
            "materialize", "--all", "--bg", "--at", "06:00",
            "--workflows-dir", str(tmp_path / "contracts"),
            "--data-dir", str(data),
        ])
        assert res.exit_code == 0, res.output
        args = install_calls[0]["materialize_args"]
        cache_idx = args.index("--cache-dir")
        cache_val = Path(args[cache_idx + 1])
        # Default derivation: <data-dir>/.cache/github (absolute).
        assert cache_val == (data.resolve() / ".cache" / "github"), (
            f"expected derived <data-dir>/.cache/github, got {cache_val}"
        )

    def test_bg_at_explicit_cache_dir_is_honored(
        self, tmp_path, monkeypatch,
    ):
        """Power-user override path: an explicit --cache-dir wins
        over the data-dir-derived default."""
        install_calls, _ = _patch_bg(monkeypatch)
        custom_cache = tmp_path / "elsewhere" / "cache"

        res = CliRunner().invoke(cli, [
            "materialize", "--all", "--bg", "--at", "06:00",
            "--workflows-dir", str(tmp_path / "contracts"),
            "--data-dir", str(tmp_path / "data"),
            "--cache-dir", str(custom_cache),
        ])
        assert res.exit_code == 0, res.output
        args = install_calls[0]["materialize_args"]
        cache_idx = args.index("--cache-dir")
        assert Path(args[cache_idx + 1]) == custom_cache.resolve()

    def test_bg_at_with_single_workflow_name(
        self, tmp_path, monkeypatch,
    ):
        install_calls, _ = _patch_bg(monkeypatch)
        res = CliRunner().invoke(cli, [
            "materialize", "sk", "--bg", "--at", "14:30",
            "--workflows-dir", str(tmp_path / "contracts"),
            "--data-dir", str(tmp_path / "data"),
        ])
        assert res.exit_code == 0, res.output
        assert len(install_calls) == 1
        kw = install_calls[0]
        assert kw["hour"] == 14
        assert kw["minute"] == 30
        args = kw["materialize_args"]
        assert "sk" in args
        assert "--all" not in args


class TestStopSchedule:
    def test_bg_stop_invokes_dispatcher_and_does_not_install(
        self, tmp_path, monkeypatch,
    ):
        install_calls, stop_calls = _patch_bg(monkeypatch)
        res = CliRunner().invoke(cli, [
            "materialize", "--bg", "--stop",
            "--workflows-dir", str(tmp_path / "contracts"),
            "--data-dir", str(tmp_path / "data"),
        ])
        assert res.exit_code == 0, res.output
        assert install_calls == []
        assert stop_calls == [None]
