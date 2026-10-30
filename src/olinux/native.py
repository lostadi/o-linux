"""Install the o-linux command interface on a Linux host without creating a VM."""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import platform
import shlex
import sys
import time

from .config import state_dir
from .install import (atomic_write, check_launcher, install_app, install_launcher,
                      launcher_text, record_transaction)


def make_config(old=None):
    old = old or {}
    home = str(Path.home().resolve())
    user = getpass.getuser()
    if any(character in home for character in "\n\r\0"):
        raise ValueError("The home directory cannot contain line separators")
    identity = {"provider": "native", "host_home": home, "host_user": user,
                "guest_home": home, "guest_user": user}
    if old:
        if old.get("provider", "multipass") != "native":
            raise RuntimeError("Existing configuration uses a different provider; use a separate O_LINUX_STATE")
        for field, expected in identity.items():
            if old.get(field) != expected:
                raise RuntimeError("Existing configuration differs at " + field + "; use a separate O_LINUX_STATE")
    result = dict(old)
    result.update(identity, schema=1)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="show configuration and paths without making changes")
    parser.add_argument("--replace-launcher", action="store_true", help="back up and replace existing linux/o-linux commands")
    args = parser.parse_args(argv)
    transaction = None
    try:
        state = state_dir()
        path = state / "config.json"
        old = json.loads(path.read_text()) if path.exists() else None
        config = make_config(old)
        targets = [Path.home() / ".local/bin" / name for name in ("linux", "o-linux")]
        if args.plan:
            print(json.dumps({"state": str(state), "configuration": config,
                              "launchers": [str(target) for target in targets],
                              "replace_launcher": args.replace_launcher}, indent=2))
            return 0
        if platform.system() != "Linux":
            raise RuntimeError("The native provider requires Linux")
        if os.geteuid() == 0:
            raise RuntimeError("Run setup as your normal Linux account, not through sudo")
        launcher = launcher_text(state)
        for target in targets:
            check_launcher(target, launcher, args.replace_launcher)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (state / "install.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            repository = Path(__file__).resolve().parents[2]
            transaction = {"schema": 1, "provider": "native", "state": str(state),
                           "started_at": time.time(),
                           "retry_argv": [sys.executable, str(repository / "scripts/install.py"), "--replace-launcher"],
                           "retry_environment": {"O_LINUX_STATE": str(state)},
                           "recovery": "Rerun retry_argv with retry_environment. Existing application and launcher backups are retained; no VM or package installation is involved."}
            record_transaction(state, transaction, "local-app-and-config")
            install_app(repository, state)
            atomic_write(path, json.dumps(config, indent=2) + "\n")
            record_transaction(state, transaction, "launchers")
            for target in targets:
                install_launcher(target, launcher, state, args.replace_launcher)
            record_transaction(state, transaction, "complete", status="complete")
        print("Installed native Linux provider. Run o-linux COMMAND or linux COMMAND; run linux for a shell.")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print("o-linux setup: " + str(error), file=sys.stderr)
        if transaction is not None:
            try:
                record_transaction(state, transaction, transaction["phase"], error=error)
            except OSError:
                pass
            print("Setup is incomplete; changes and backups were retained. Retry with:\n"
                  + "O_LINUX_STATE=" + shlex.quote(str(state)) + " "
                  + shlex.join(transaction["retry_argv"])
                  + "\nTransaction: " + str(state / "install-transaction.json"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
