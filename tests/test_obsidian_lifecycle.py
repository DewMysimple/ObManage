import json
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest

from obmanage.management import obsidian_lifecycle as lifecycle
from obmanage.models import SyncCancelled


class FakeProcess:
    def __init__(self, pid=1, running=True, force_succeeds=True):
        self.pid = pid
        self.alive = running
        self.force_succeeds = force_succeeds
        self.terminated = False
        self.closed = False

    def running(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        if self.force_succeeds:
            self.alive = False

    def close(self):
        self.closed = True


def inventory(monkeypatch, *observations):
    values = iter(observations)
    monkeypatch.setattr(lifecycle, "_windows_processes", lambda: next(values))


def test_graceful_shutdown_waits_and_rechecks_without_force(monkeypatch):
    process = FakeProcess()
    inventory(monkeypatch, (process,), ())

    def close_windows(processes):
        assert processes == (process,)
        process.alive = False

    monkeypatch.setattr(lifecycle, "_request_windows_close", close_windows)
    assert lifecycle.close_obsidian() == 1
    assert not process.terminated and process.closed


def test_windowless_process_is_forced_by_verified_handle(monkeypatch):
    process = FakeProcess()
    inventory(monkeypatch, (process,), ())
    monkeypatch.setattr(lifecycle, "_request_windows_close", lambda _: None)
    events = []
    assert lifecycle.close_obsidian(graceful_timeout=0, progress=events.append) == 1
    assert process.terminated and process.closed
    assert "强制" in events[-1].message


def test_force_timeout_and_respawn_prevent_synchronization(monkeypatch):
    process = FakeProcess(force_succeeds=False)
    inventory(monkeypatch, (process,))
    monkeypatch.setattr(lifecycle, "_request_windows_close", lambda _: None)
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="限定时间"):
        lifecycle.close_obsidian(graceful_timeout=0, force_timeout=0)
    assert process.closed

    process = FakeProcess()
    respawn = FakeProcess(pid=2)
    inventory(monkeypatch, (process,), (respawn,))
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="重新启动"):
        lifecycle.close_obsidian(graceful_timeout=0)
    assert process.closed and respawn.closed


def test_empty_inventory_ignores_stale_saved_flags_but_rechecks(monkeypatch):
    inventory(monkeypatch, (), ())
    monkeypatch.setattr(lifecycle, "_request_windows_close", lambda _: pytest.fail("no windows to close"))
    assert lifecycle.close_obsidian() == 0


def test_unreliable_inventory_is_not_treated_as_closed(monkeypatch):
    def denied():
        raise OSError("access denied")

    monkeypatch.setattr(lifecycle, "_windows_processes", denied)
    monkeypatch.setattr(lifecycle, "_request_windows_close", lambda _: pytest.fail("must stop"))
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="access denied"):
        lifecycle.close_obsidian()
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="access denied"):
        lifecycle.assert_obsidian_closed()


def test_cancellation_during_graceful_exit_prevents_force(monkeypatch):
    cancel = Event()
    process = FakeProcess()
    inventory(monkeypatch, (process,))
    monkeypatch.setattr(lifecycle, "_request_windows_close", lambda _: cancel.set())
    with pytest.raises(SyncCancelled):
        lifecycle.close_obsidian(cancel=cancel)
    assert not process.terminated and process.closed
    monkeypatch.setattr(lifecycle, "_windows_processes", lambda: pytest.fail("pre-cancelled"))
    with pytest.raises(SyncCancelled):
        lifecycle.close_obsidian(cancel=cancel)


def test_termination_uses_owned_handle_never_recycled_pid():
    calls = []

    class Kernel:
        def WaitForSingleObject(self, handle, timeout):
            calls.append(("wait", handle))
            return 258

        def TerminateProcess(self, handle, code):
            calls.append(("terminate", handle))
            return True

        def CloseHandle(self, handle):
            calls.append(("close", handle))

    process = lifecycle._WindowsProcess(Kernel(), 12345, 99, r"C:\Apps\Obsidian.exe")
    process.terminate()
    process.close()
    assert calls == [("wait", 12345), ("terminate", 12345), ("close", 12345)]


def registry(path, entries):
    path.write_text(json.dumps({"vaults": entries}, ensure_ascii=False), encoding="utf-8")
    return path


def test_opening_matches_full_paths_to_unique_ids_and_continues_after_failure(tmp_path, monkeypatch):
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
    failures = lifecycle.open_vaults((first, first, second), config_path=config)
    assert len(failures) == 1 and "protocol unavailable" in failures[0]
    assert len(requested) == 2
    assert [parse_qs(urlsplit(uri).query)["vault"][0] for uri in requested] == [
        "0123456789abcdef", "abcdef0123456789"]
    assert all("同名仓库" not in uri and " " not in uri for uri in requested)
    assert config.read_bytes() == config_before
    assert sorted(p.name for p in first.iterdir()) == [".obsidian"]
    assert sorted(p.name for p in second.iterdir()) == [".obsidian"]


def test_invalid_or_missing_vault_is_not_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_open_uri", lambda _: pytest.fail("invalid vault must not open"))
    failures = lifecycle.open_vaults((tmp_path / "missing",))
    assert len(failures) == 1 and "无法自动打开" in failures[0]


@pytest.mark.parametrize("contents,detail", [
    ("{broken", "无法安全读取"),
    ('{"vaults": []}', "vaults 对象"),
    ('{"vaults": {"0123456789abcdef": null}}', "有效路径"),
    ('{"vaults": {"0123456789abcdef": {"path": "relative"}}}', "相对路径"),
    ('{"vaults": {}}', "先在 Obsidian 中打开"),
])
def test_invalid_or_missing_registration_never_opens_another_vault(tmp_path, monkeypatch, contents, detail):
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    config = tmp_path / "obsidian.json"
    config.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(lifecycle, "_open_uri", lambda _: pytest.fail("no reliable exact ID"))
    failures = lifecycle.open_vaults((root,), config_path=config)
    assert len(failures) == 1 and detail in failures[0]
    assert config.read_text(encoding="utf-8") == contents


def test_duplicate_path_and_invalid_id_fail_closed(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    config = registry(tmp_path / "obsidian.json", {
        "0123456789abcdef": {"path": str(root)},
        "abcdef0123456789": {"path": str(root)},
    })
    monkeypatch.setattr(lifecycle, "_open_uri", lambda _: pytest.fail("ambiguous or malformed ID"))
    assert "多个" in lifecycle.open_vaults((root,), config_path=config)[0]
    registry(config, {"arbitrary # & vault name": {"path": str(root)}})
    assert "ID 无效" in lifecycle.open_vaults((root,), config_path=config)[0]


def test_missing_unreadable_or_changed_registry_leaves_success_separate(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    config = tmp_path / "obsidian.json"
    monkeypatch.setattr(lifecycle, "_open_uri", lambda _: pytest.fail("unreliable registry"))
    assert "先在 Obsidian 中打开" in lifecycle.open_vaults((root,), config_path=config)[0]
    registry(config, {"0123456789abcdef": {"path": str(root)}})

    def changed(*args):
        raise OSError("配置文件在读取期间被替换或改变")

    monkeypatch.setattr(lifecycle, "_read_stable_file", changed)
    assert "被替换或改变" in lifecycle.open_vaults((root,), config_path=config)[0]


def test_default_registry_location_uses_appdata_without_writing(tmp_path, monkeypatch):
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
    assert lifecycle.open_vaults((root,)) == ()
    assert requested == ["obsidian://open?vault=0123456789abcdef"]
    assert config.read_bytes() == before


class NativeFunction:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def native_inventory(monkeypatch, *, executable=r"C:\Apps\Obsidian.exe", denied=False):
    """A fake Toolhelp API tests name filtering and image validation, never the OS."""
    import ctypes

    error = [0]
    closed = []
    opened = []
    names = iter(("Obsidian.exe.bak", "Obsidian.exe"))

    def next_process(handle, entry):
        name = next(names, None)
        if name is None:
            error[0] = 18
            return False
        entry._obj.szExeFile = name
        entry._obj.th32ProcessID = 42
        return True

    def open_process(access, inherit, pid):
        opened.append(pid)
        if denied:
            error[0] = 5
            return None
        return 900

    def query_image(handle, flags, buffer, length):
        buffer.value = executable
        return True

    class Kernel:
        CreateToolhelp32Snapshot = NativeFunction(lambda *_: 800)
        Process32FirstW = NativeFunction(next_process)
        Process32NextW = NativeFunction(next_process)
        OpenProcess = NativeFunction(open_process)
        QueryFullProcessImageNameW = NativeFunction(query_image)
        WaitForSingleObject = NativeFunction(lambda *_: 258)
        TerminateProcess = NativeFunction(lambda *_: pytest.fail("enumeration must never terminate"))
        CloseHandle = NativeFunction(lambda handle: closed.append(handle))

    monkeypatch.setattr(lifecycle.os, "name", "nt")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Kernel(), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error[0], raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda value: OSError(value, "native failure"), raising=False)
    return opened, closed


def test_native_inventory_ignores_similar_names_and_verifies_full_image(monkeypatch):
    opened, closed = native_inventory(monkeypatch)
    processes = lifecycle._windows_processes()
    assert opened == [42] and closed == [800]
    assert processes[0].handle == 900
    processes[0].close()
    assert closed == [800, 900]


def test_native_inventory_fails_closed_for_denied_or_reused_identity(monkeypatch):
    opened, closed = native_inventory(monkeypatch, denied=True)
    with pytest.raises(OSError):
        lifecycle._windows_processes()
    assert opened == [42] and closed == [800]
    opened, closed = native_inventory(monkeypatch, executable=r"C:\Other\not-obsidian.exe")
    with pytest.raises(lifecycle.ObsidianLifecycleError, match="身份发生变化"):
        lifecycle._windows_processes()
    assert closed == [900, 800]
