"""recast-server as its own install: separate config folder, first-run setup from the environment, import
from the terminal app on the same machine."""
import json

from recast import config
from recast.config import Config
from recast.server import import_terminal_settings, seed_from_env


def test_server_profile_has_its_own_config_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("RECAST_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(config, "_profile", config.APP)
    terminal = config.config_dir()
    monkeypatch.setattr(config, "_profile", config.SERVER)
    assert config.config_dir() != terminal and config.config_dir().name == "recast-server"
    assert config.config_dir(config.APP) == terminal
    monkeypatch.setenv("RECAST_HOME", str(tmp_path / "cfg"))
    assert config.config_dir() == tmp_path / "cfg"                       # Docker: /config


def test_env_fills_in_first_run(monkeypatch, home, tmp_path):
    (tmp_path / "tv").mkdir()
    (tmp_path / "movies").mkdir()
    monkeypatch.setenv("RECAST_LIBRARIES", f"{tmp_path / 'tv'}, Films={tmp_path / 'movies'}, /nope/missing")
    monkeypatch.setenv("RECAST_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("RECAST_SONARR_URL", "http://sonarr:8989")
    monkeypatch.setenv("RECAST_SONARR_API_KEY", "abc")
    cfg = Config.load()
    seed_from_env(cfg)
    assert [(r.name, r.path) for r in cfg.roots] == [("Tv", str(tmp_path / "tv")), ("Films", str(tmp_path / "movies"))]
    assert cfg.scratch == str(tmp_path / "scratch") and (tmp_path / "scratch").is_dir()
    assert cfg.sonarr.url == "http://sonarr:8989" and cfg.sonarr.api_key == "abc"
    assert Config.load().roots                                            # saved
    cfg.sonarr.url = "http://changed-in-the-ui"
    cfg.roots = cfg.roots[:1]
    seed_from_env(cfg)                                                    # the web app's changes win
    assert cfg.sonarr.url == "http://changed-in-the-ui" and len(cfg.roots) == 1


def test_imports_the_terminal_apps_settings_once(monkeypatch, tmp_path):
    monkeypatch.delenv("RECAST_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(config, "_profile", config.SERVER)
    term, srv = config.config_dir(config.APP), config.config_dir()
    (term / "presets").mkdir(parents=True)
    (term / "config.json").write_text(json.dumps({"roots": [{"name": "TV", "path": "/media/tv"}],
                                                  "scratch": "/Users/me/Documents/recast", "auto_mode": "on"}))
    (term / "presets" / "Mine.json").write_text("{}")
    (term / "probe-cache.json").write_text("{}")
    (term / "state.json").write_text("{}")
    assert import_terminal_settings()
    cfg = Config.load()
    assert cfg.roots[0].path == "/media/tv" and cfg.auto_mode == "off"    # automation never starts by itself
    assert cfg.scratch != "/Users/me/Documents/recast"
    assert (srv / "presets" / "Mine.json").exists() and (srv / "probe-cache.json").exists()
    assert not (srv / "state.json").exists()                              # queue + history stay with the app
    assert not import_terminal_settings()                                 # only the first time


def test_update_uses_whatever_installed_recast(monkeypatch, tmp_path):
    from recast import update
    rel = {"version": "9.9.9", "tag": "v9.9.9", "wheel": "https://example/recast-9.9.9-py3-none-any.whl"}
    (tmp_path / "uv-receipt.toml").write_text("")
    monkeypatch.setattr(update.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/bin/" + name)
    assert update.install_command(rel) == ["uv", "tool", "install", "--force", rel["wheel"]]
    (tmp_path / "uv-receipt.toml").unlink()
    (tmp_path / "pipx_metadata.json").write_text("{}")
    assert update.install_command(rel)[:3] == ["pipx", "install", "--force"]
    (tmp_path / "pipx_metadata.json").unlink()
    assert update.install_command({**rel, "wheel": ""})[-1] == "recast @ git+https://github.com/awpsec/recast@v9.9.9"
    assert update.parse_version("v0.10.0") > update.parse_version("0.9.3")
