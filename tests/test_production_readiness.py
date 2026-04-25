from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def test_gunicorn_prefers_railway_port_env(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "4321")
    spec = spec_from_file_location("gunicorn_conf_test", Path("gunicorn.conf.py"))
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.bind.endswith(":4321")
