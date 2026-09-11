from __future__ import annotations

import json

from obmanage.settings import AppSettings, SETTINGS_SCHEMA_VERSION, SettingsDocument, SettingsStore


def test_legacy_flat_settings_migrate_once_without_losing_mirror_state(tmp_path):
    store = SettingsStore(tmp_path)
    legacy = {
        "source": r"C:\工作仓库",
        "target": r"H:\移动仓库",
        "direction": "to_portable",
        "recent_targets": [r"H:\移动仓库"],
        "schedule_enabled": True,
        "bound_source": r"C:\工作仓库",
        "bound_target": r"H:\移动仓库",
    }
    store.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    document = store.load_document()
    assert document.mirror.source == legacy["source"]
    assert document.mirror.target == legacy["target"]
    assert document.mirror.schedule_enabled
    document.selected_page = "statistics"
    document.feature("statistics")["root"] = r"D:\仓库集合"
    store.save_document(document)

    backup = tmp_path / "settings.v1.backup.json"
    assert json.loads(backup.read_text(encoding="utf-8")) == legacy
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SETTINGS_SCHEMA_VERSION
    assert raw["features"]["mirror"]["source"] == legacy["source"]
    assert raw["features"]["statistics"]["root"] == r"D:\仓库集合"
    store.save_document(document)
    assert json.loads(backup.read_text(encoding="utf-8")) == legacy


def test_mirror_compatibility_save_preserves_feature_namespaces(tmp_path):
    store = SettingsStore(tmp_path)
    document = SettingsDocument()
    document.feature("trash_cleanup")["root"] = r"C:\仓库集合"
    store.save_document(document)

    mirror = store.load()
    mirror.source = r"D:\新来源"
    store.save(mirror)

    restored = SettingsStore(tmp_path).load_document()
    assert restored.mirror.source == r"D:\新来源"
    assert restored.features["trash_cleanup"] == {"root": r"C:\仓库集合"}


def test_invalid_single_feature_does_not_reset_mirror(tmp_path):
    mirror = AppSettings(source=r"C:\来源", target=r"H:\目标")
    raw = {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "ui": {"selected_page": "mirror"},
        "features": {
            "mirror": mirror.__dict__,
            "statistics": "damaged",
            "templater": {"source": r"C:\模板"},
        },
    }
    store = SettingsStore(tmp_path)
    store.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    document = store.load_document()
    assert document.mirror.source == r"C:\来源"
    assert document.features["statistics"] == {}
    assert document.features["templater"] == {"source": r"C:\模板"}
    assert "statistics" in store.last_error


def test_invalid_ui_namespace_does_not_reset_feature_namespaces(tmp_path):
    mirror = AppSettings(source=r"D:\来源", target=r"H:\目标")
    raw = {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "ui": "damaged",
        "features": {
            "mirror": mirror.__dict__,
            "statistics": {"root": r"D:\仓库集合"},
        },
    }
    store = SettingsStore(tmp_path)
    store.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    document = store.load_document()

    assert document.selected_page == "mirror"
    assert document.mirror.source == r"D:\来源"
    assert document.mirror.target == r"H:\目标"
    assert document.features["statistics"] == {"root": r"D:\仓库集合"}
    assert "界面设置" in store.last_error


def test_invalid_features_namespace_preserves_ui_namespace(tmp_path):
    raw = {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "ui": {"selected_page": "statistics"},
        "features": "damaged",
    }
    store = SettingsStore(tmp_path)
    store.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    document = store.load_document()

    assert document.selected_page == "statistics"
    assert document.mirror == AppSettings()
    assert document.features == {}
    assert "功能设置" in store.last_error


def test_unknown_schema_fails_closed_to_defaults(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_text(
        json.dumps({"schema_version": 999, "source": r"X:\unexpected"}),
        encoding="utf-8",
    )

    document = store.load_document()
    assert document.mirror == AppSettings()
    assert "不支持" in store.last_error
