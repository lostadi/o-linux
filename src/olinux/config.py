"""Installation-local configuration. No machine identity belongs in source."""
import json
import os
from pathlib import Path
import shutil
import subprocess


def state_dir():
    return Path(os.environ.get("O_LINUX_STATE", str(Path.home() / ".local/share/o-linux"))).expanduser().resolve()


def config_path():
    return state_dir() / "config.json"


def load_config():
    path = config_path()
    if not path.is_file():
        raise RuntimeError("o-linux is not configured; run ./setup.sh from the checkout")
    result = json.loads(path.read_text())
    if not isinstance(result, dict) or result.get("provider", "multipass") not in ("multipass", "native"):
        raise RuntimeError("Unsupported o-linux provider in " + str(path))
    required = ("host_home", "host_user", "guest_user", "guest_home")
    if isinstance(result, dict) and result.get("provider", "multipass") == "multipass":
        required += ("instance", "multipass")
    if not isinstance(result, dict) or any(not result.get(key) for key in required):
        raise RuntimeError("Incomplete o-linux configuration: " + str(path))
    return result


def command(name, config=None):
    configured = (config or {}).get(name.replace("-", "_"))
    value = configured or shutil.which(name)
    if not value:
        raise RuntimeError(name + " is required and was not found on PATH")
    return value


def run(argv, timeout=30, check=True, data=None):
    result = subprocess.run(argv, input=data, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed ({result.returncode}): "
                           + (result.stderr.strip() or result.stdout.strip()))
    return result


def vm_state(config):
    rows = json.loads(run([command("multipass", config), "list", "--format", "json"]).stdout)["list"]
    return next((row for row in rows if row["name"] == config["instance"]), None)
