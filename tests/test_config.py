"""Storage-location config, including the read-only serverless (Vercel) case."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config
from conftest import ROOT, make_config
from app import create_app

_PATH_VARS = ("INSTANCE_DIR", "DATABASE_PATH", "UPLOAD_FOLDER", "BACKUP_FOLDER", "LOG_FOLDER",
              "LOG_TO_FILE")


def test_local_dev_uses_instance_dir_next_to_code(tmp_path):
    assert not config.is_serverless({}, tmp_path)
    assert config.default_instance_dir({}, tmp_path) == tmp_path / "instance"


@pytest.mark.parametrize("env", [
    {"VERCEL": "1"},
    {"VERCEL_ENV": "production"},
    # Vercel without "system environment variables" exposed: no VERCEL=1,
    # but the Lambda runtime variables are always present.
    {"LAMBDA_TASK_ROOT": "/var/task"},
    {"AWS_LAMBDA_FUNCTION_NAME": "wsgi"},
])
def test_serverless_env_uses_temp_dir(tmp_path, env):
    assert config.is_serverless(env, tmp_path)
    instance = config.default_instance_dir(env, tmp_path)
    assert instance.parent == Path(config.tempfile.gettempdir())
    assert tmp_path not in instance.parents


def test_read_only_code_dir_uses_temp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config.os, "access", lambda path, mode: False)
    assert config.is_serverless({}, tmp_path)
    assert config.default_instance_dir({}, tmp_path).parent == Path(config.tempfile.gettempdir())
    # A read-only code dir alone is not proof of a trusted proxy in front.
    assert not config.on_serverless_platform({})


@pytest.mark.parametrize("serverless_var", ["VERCEL", "LAMBDA_TASK_ROOT"])
def test_wsgi_import_on_serverless_writes_only_to_temp_dir(tmp_path, serverless_var):
    """Reproduces the Vercel cold start: ``import wsgi`` with default paths."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("VERCEL", "AWS_LAMBDA", "LAMBDA_")) and k not in _PATH_VARS}
    env.update({
        serverless_var: "/var/task" if serverless_var == "LAMBDA_TASK_ROOT" else "1",
        "SECRET_KEY": "x" * 64,
        "TMPDIR": str(tmp_path), "TEMP": str(tmp_path), "TMP": str(tmp_path),
    })
    script = (
        "import json, wsgi; c = wsgi.app.config; "
        "print(json.dumps({k: str(c[k]) for k in "
        "('DATABASE', 'UPLOAD_FOLDER', 'BACKUP_FOLDER', 'LOG_FOLDER', 'LOG_TO_FILE')}))"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    paths = json.loads(result.stdout.strip().splitlines()[-1])

    instance = tmp_path.resolve() / "part-manager"
    for key in ("DATABASE", "UPLOAD_FOLDER", "BACKUP_FOLDER", "LOG_FOLDER"):
        assert Path(paths[key]).resolve().is_relative_to(instance), (key, paths[key])
    assert paths["LOG_TO_FILE"] == "False"
    assert (instance / "uploads").is_dir()
    assert (instance / "trolley_manager.db").is_file()
    assert not (instance / "logs").exists()
    # The startup log warns that this storage is not persistent.
    assert "NOT persistent" in result.stderr


def test_log_folder_not_created_when_file_logging_disabled(tmp_path):
    app = create_app(make_config(tmp_path, LOG_TO_FILE=False))
    assert Path(app.config["UPLOAD_FOLDER"]).is_dir()
    assert not Path(app.config["LOG_FOLDER"]).exists()


def test_production_still_requires_secret_key(tmp_path):
    cfg = make_config(tmp_path, SECRET_KEY=None)
    cfg["TESTING"] = False
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(cfg)
