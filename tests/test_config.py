import pytest
import yaml

from jsn.config import load_config


@pytest.mark.parametrize("gmail", [None, "invalid", []])
def test_invalid_gmail_section_reports_settings_source(tmp_path, monkeypatch, gmail):
    path = tmp_path / "settings.yaml"
    settings = {
        "profile": {"skills": ["Python"]},
        "filters": {}, "llm": {}, "discord": {}, "database": {},
    }
    if gmail is not None:
        settings["gmail"] = gmail
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    monkeypatch.setenv("JSN_CONFIG_PATH", str(path))

    with pytest.raises(ValueError) as error:
        load_config()

    assert str(path) in str(error.value)
    assert "top-level gmail mapping" in str(error.value)
    assert "JSN_SETTINGS_YAML" in str(error.value)
