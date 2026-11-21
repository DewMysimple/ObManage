import json
from dataclasses import replace
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest

from obmanage.management import obsidian_lifecycle as lifecycle
from obmanage.management import running_vaults as runtime
from obmanage.management.models import ManagementIssue, OpenedVaultCandidates
from obmanage.models import SyncCancelled


def setup_vaults(tmp_path, monkeypatch):
    roots = {}
    windows = {}
    for handle, name in enumerate(("A", "B", "C"), 1):
        root = tmp_path / name
        (root / ".obsidian").mkdir(parents=True)
        roots[name] = str(root)
        windows[handle] = runtime.ObsidianWindow(handle, 123, f"note - {name} - Obsidian v1.10.6",
                                                 r"C:\Apps\Obsidian.exe", 501, 12, "Chrome_WidgetWin_1")
    monkeypatch.setattr(runtime, "obsidian_windows", lambda: tuple(windows.values()))
    monkeypatch.setattr(runtime, "read_registered_vault_candidates", lambda _: OpenedVaultCandidates(tuple(roots.values())))
    return roots, windows


def test_only_target_and_owned_popup_close_while_shared_process_windows_stay(tmp_path, monkeypatch):
    roots, windows = setup_vaults(tmp_path, monkeypatch)
    windows[4] = replace(windows[2], handle=4, title="Pop-out", owner=2, visible=False)
    posted = []

    def close(window):
        posted.append(window.handle)
        del windows[window.handle]
        if window.handle == 2:
            del windows[4]

    monkeypatch.setattr(lifecycle, "post_window_close", close)
    events = []
    assert lifecycle.close_target_sessions((roots["B"], roots["B"]), on_closed=events.append) == (roots["B"],)
    assert events == [roots["B"]] and posted == [2]
    assert set(windows) == {1, 3}
    lifecycle.assert_target_sessions_closed((roots["B"],))
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="仍在运行"):
        lifecycle.assert_target_sessions_closed((roots["A"],))


def test_partial_closure_survives_later_failure_and_leaves_other_vault(tmp_path, monkeypatch):
    roots, windows = setup_vaults(tmp_path, monkeypatch)
    closed = []

    def post(window):
        if window.handle == 2:
            raise OSError("access denied")
        del windows[window.handle]

    monkeypatch.setattr(lifecycle, "post_window_close", post)
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="access denied"):
        lifecycle.close_target_sessions((roots["A"], roots["B"]), on_closed=closed.append)
    assert closed == [roots["A"]] and set(windows) == {2, 3}


def test_target_windows_can_settle_between_independent_close_requests(tmp_path, monkeypatch):
    roots, windows = setup_vaults(tmp_path, monkeypatch)
    windows[4] = replace(windows[2], handle=4)
    read = lifecycle._runtime
    posted = []
    transitions = []

    def snapshot(*args, **kwargs):
        if len(posted) == 1 and not transitions:
            transitions.append(True)
            return runtime.RuntimeSnapshot(unresolved=(runtime.UnresolvedWindow(ManagementIssue(
                "runtime_changed", "Windows changed while closing the main window")),))
        return read(*args, **kwargs)

    def post(window):
        posted.append(window.handle)
        del windows[window.handle]

    monkeypatch.setattr(lifecycle, "_runtime", snapshot)
    monkeypatch.setattr(lifecycle, "post_window_close", post)
    assert lifecycle.close_target_sessions((roots["B"],)) == (roots["B"],)
    assert posted == [2, 4] and set(windows) == {1, 3}


def test_cancel_after_post_confirms_the_closed_session_before_cancelling(tmp_path, monkeypatch):
    roots, windows = setup_vaults(tmp_path, monkeypatch)
    cancel = Event()
    closed = []

    def post(window):
        assert window.handle == 1
        del windows[window.handle]
        cancel.set()

    monkeypatch.setattr(lifecycle, "post_window_close", post)
    with pytest.raises(SyncCancelled):
        lifecycle.close_target_sessions((roots["A"], roots["B"]), cancel=cancel, on_closed=closed.append)
    assert closed == [roots["A"]] and set(windows) == {2, 3}


def test_window_never_assumed_closed_after_post_and_respawn_blocks_commit(tmp_path, monkeypatch):
    roots, windows = setup_vaults(tmp_path, monkeypatch)
    closed = []
    monkeypatch.setattr(lifecycle, "post_window_close", lambda _: None)
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="限定时间"):
        lifecycle.close_target_sessions((roots["B"],), graceful_timeout=0, on_closed=closed.append)
    assert not closed

    def reopen(window):
        windows[2] = replace(window, process_started=502)

    monkeypatch.setattr(lifecycle, "post_window_close", reopen)
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="重新打开"):
        lifecycle.close_target_sessions((roots["B"],), on_closed=closed.append)
    assert closed == [roots["B"]]
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="重新打开"):
        lifecycle.assert_target_sessions_closed((roots["B"],))


def test_unknown_hidden_window_and_inaccessible_runtime_block_before_any_post(tmp_path, monkeypatch):
    roots, windows = setup_vaults(tmp_path, monkeypatch)
    windows[4] = replace(windows[2], handle=4, title="", visible=False)
    monkeypatch.setattr(lifecycle, "post_window_close", lambda _: pytest.fail("unknown target state"))
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="无法识别"):
        lifecycle.close_target_sessions((roots["B"],))

    def denied():
        raise OSError("access denied")

    monkeypatch.setattr(runtime, "obsidian_windows", denied)
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="access denied"):
        lifecycle.assert_target_sessions_closed((roots["B"],))


def test_verified_original_closure_is_recorded_even_if_new_window_is_unknown(tmp_path, monkeypatch):
    roots, windows = setup_vaults(tmp_path, monkeypatch)
    closed = []

    def post(window):
        del windows[window.handle]
        windows[4] = replace(window, handle=4, title="Unrecognized window")

    monkeypatch.setattr(lifecycle, "post_window_close", post)
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="无法识别"):
        lifecycle.close_target_sessions((roots["B"],), graceful_timeout=0, on_closed=closed.append)
    assert closed == [roots["B"]]


def test_changed_directory_binding_prevents_any_window_close(tmp_path, monkeypatch):
    roots, _ = setup_vaults(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle, "post_window_close", lambda _: pytest.fail("identity changed"))
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="目录身份已改变"):
        lifecycle.close_target_sessions((roots["B"],), expected_identities={roots["B"]: ("dir", 0, 0)})


def test_reused_window_identity_is_rejected_before_any_native_post(tmp_path, monkeypatch):
    _, windows = setup_vaults(tmp_path, monkeypatch)
    expected = windows[1]

    class API:
        def owner_ids(self, hwnd):
            return expected.process_id, expected.thread_id

        def process_identity(self, pid):
            return expected.executable, 502

        def inspect(self, hwnd, pid, thread, process):
            return replace(expected, process_started=process[1])

    monkeypatch.setattr(runtime, "_WindowsAPI", API)
    with pytest.raises(OSError, match="身份"):
        runtime.post_window_close(expected)


def test_request_checks_running_state_and_exact_vault_identity(tmp_path, monkeypatch):
    roots, windows = setup_vaults(tmp_path, monkeypatch)
    config = registry(tmp_path / "obsidian.json", {"0123456789abcdef": {"path": roots["B"]}})
    requested = []
    monkeypatch.setattr(lifecycle, "_open_uri", requested.append)
    expected_identity = runtime.vault_identity(roots["B"])
    assert lifecycle.request_open_target(roots["B"], config_path=config, expected_identity=expected_identity) is False
    del windows[2]
    assert lifecycle.request_open_target(roots["B"], config_path=config, expected_identity=expected_identity) is True
    assert requested == ["obsidian://open?vault=0123456789abcdef"]
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="身份"):
        lifecycle.request_open_target(roots["B"], config_path=config, expected_identity=("dir", 0, 0))
    assert len(requested) == 1


def registry(path, entries):
    path.write_text(json.dumps({"vaults": entries}, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def no_running_windows(monkeypatch):
    monkeypatch.setattr(runtime, "obsidian_windows", lambda: ())


def test_opening_identifies_registered_vault_by_full_path(tmp_path, monkeypatch, no_running_windows):
    first = tmp_path / "A # &" / "同名仓库"
    second = tmp_path / "B" / "同名仓库"
    for root in (first, second):
        (root / ".obsidian").mkdir(parents=True)
    config = registry(tmp_path / "obsidian.json", {
        "0123456789abcdef": {"path": str(first), "open": True},
        "abcdef0123456789": {"path": str(second), "open": False},
    })
    config_before = config.read_bytes()
    requested = []

    def launch(uri):
        requested.append(uri)
        if len(requested) == 1:
            raise OSError("protocol unavailable")

    monkeypatch.setattr(lifecycle, "_open_uri", launch)
    with pytest.raises(OSError, match="protocol unavailable"):
        lifecycle.request_open_target(first, config_path=config)
    assert lifecycle.request_open_target(second, config_path=config)
    assert len(requested) == 2
    assert [parse_qs(urlsplit(uri).query)["vault"][0] for uri in requested] == [
        "0123456789abcdef", "abcdef0123456789"]
    assert all("同名仓库" not in uri and " " not in uri for uri in requested)
    assert config.read_bytes() == config_before
    assert sorted(p.name for p in first.iterdir()) == [".obsidian"]
    assert sorted(p.name for p in second.iterdir()) == [".obsidian"]


def test_invalid_or_missing_vault_is_not_opened(tmp_path, monkeypatch, no_running_windows):
    monkeypatch.setattr(lifecycle, "_open_uri", lambda _: pytest.fail("invalid vault must not open"))
    with pytest.raises((OSError, lifecycle.ObsidianLifecycleError)):
        lifecycle.request_open_target(tmp_path / "missing")


@pytest.mark.parametrize("contents,detail", [
    ("{broken", "无法安全读取"),
    ('{"vaults": []}', "vaults 对象"),
    ('{"vaults": {"0123456789abcdef": null}}', "有效路径"),
    ('{"vaults": {"0123456789abcdef": {"path": "relative"}}}', "相对路径"),
    ('{"vaults": {}}', "先在 Obsidian 中打开"),
])
def test_invalid_or_missing_registration_never_opens_another_vault(tmp_path, monkeypatch, contents, detail, no_running_windows):
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    config = tmp_path / "obsidian.json"
    config.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(lifecycle, "_open_uri", lambda _: pytest.fail("no reliable exact ID"))
    with pytest.raises(lifecycle.ObsidianLifecycleError, match=detail):
        lifecycle.request_open_target(root, config_path=config)
    assert config.read_text(encoding="utf-8") == contents


def test_duplicate_path_and_invalid_id_fail_closed(tmp_path, monkeypatch, no_running_windows):
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    config = registry(tmp_path / "obsidian.json", {
        "0123456789abcdef": {"path": str(root)},
        "abcdef0123456789": {"path": str(root)},
    })
    monkeypatch.setattr(lifecycle, "_open_uri", lambda _: pytest.fail("ambiguous or malformed ID"))
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="多个"):
        lifecycle.request_open_target(root, config_path=config)
    registry(config, {"arbitrary # & vault name": {"path": str(root)}})
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="ID 无效"):
        lifecycle.request_open_target(root, config_path=config)


def test_registry_id_that_is_another_vault_name_does_not_launch(tmp_path, monkeypatch, no_running_windows):
    target = tmp_path / "selected"
    other = tmp_path / "0123456789ABCDEF"
    for root in (target, other):
        (root / ".obsidian").mkdir(parents=True)
    config = registry(tmp_path / "obsidian.json", {
        "abcdef0123456789": {"path": str(other)},
        "0123456789abcdef": {"path": str(target)},
    })
    monkeypatch.setattr(lifecycle, "_open_uri", lambda _: pytest.fail("ID could resolve to other vault"))
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="另一仓库名称相同"):
        lifecycle.request_open_target(target, config_path=config)


def test_missing_unreadable_or_changed_registration_prevents_opening(tmp_path, monkeypatch, no_running_windows):
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    config = tmp_path / "obsidian.json"
    monkeypatch.setattr(lifecycle, "_open_uri", lambda _: pytest.fail("unreliable registry"))
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="先在 Obsidian 中打开"):
        lifecycle.request_open_target(root, config_path=config)
    registry(config, {"0123456789abcdef": {"path": str(root)}})

    def changed(*args):
        raise OSError("配置文件在读取期间被替换或改变")

    monkeypatch.setattr(lifecycle, "_read_stable_file", changed)
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="被替换或改变"):
        lifecycle.request_open_target(root, config_path=config)


def test_default_registry_location_uses_appdata_without_writing(tmp_path, monkeypatch, no_running_windows):
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    appdata = tmp_path / "appdata"
    (appdata / "obsidian").mkdir(parents=True)
    config = registry(appdata / "obsidian" / "obsidian.json", {
        "0123456789abcdef": {"path": str(root)},
    })
    before = config.read_bytes()
    monkeypatch.setenv("APPDATA", str(appdata))
    requested = []
    monkeypatch.setattr(lifecycle, "_open_uri", requested.append)
    assert lifecycle.request_open_target(root)
    assert requested == ["obsidian://open?vault=0123456789abcdef"]
    assert config.read_bytes() == before
