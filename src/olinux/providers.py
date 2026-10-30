"""Host and Linux execution with one argument-preserving command contract."""
import fcntl
import os
from pathlib import Path
import shlex
import subprocess
import sys

from .config import command, state_dir, vm_state


def ensure_running(config):
    if config.get("provider") == "native":
        return None
    state_dir().mkdir(parents=True, exist_ok=True)
    with (state_dir() / "start.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        info = vm_state(config)
        if not info:
            raise RuntimeError("The configured VM is missing; run setup to inspect the installation")
        if info["state"] != "Running":
            if info["state"] not in ("Stopped", "Suspended"):
                raise RuntimeError("Refusing to reset VM in state " + info["state"])
            print("Starting Linux...", file=sys.stderr)
            subprocess.run([command("multipass", config), "start", config["instance"]],
                           check=True, timeout=180, stdout=sys.stderr)
            info = vm_state(config)
        if not info or not info.get("ipv4"):
            raise RuntimeError("The Linux VM has no address")
        return info["ipv4"][0]


def effective_cwd(config, target, cwd=None):
    cwd = cwd or os.getcwd()
    if target == "host" or config.get("provider") == "native":
        return cwd
    roots = [config["host_home"]]
    if config.get("share_volumes", True): roots.append("/Volumes")
    return cwd if any(cwd == root or cwd.startswith(root + "/") for root in roots) else config["guest_home"]


def linux_argv(config, argv, cwd=None, tty=False, address=None):
    if config.get("provider") == "native":
        return list(argv)
    address = address or ensure_running(config)
    destination = effective_cwd(config, "linux", cwd)
    remote = "cd " + shlex.quote(destination) + " && exec " + shlex.join(argv)
    return [command("ssh", config), "-F", "/dev/null", "-i", str(state_dir() / "guest_id_ed25519"),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + str(state_dir() / "guest_known_hosts"),
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlias=" + config.get("guest_host_alias", "o-linux-guest"),
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-tt" if tty else "-T", config["guest_user"] + "@" + address, remote]


def execute(config, target, argv, cwd=None, timeout=None):
    if not argv or not all(isinstance(a, str) and "\0" not in a for a in argv):
        raise ValueError("A nonempty literal command argument list is required")
    actual = linux_argv(config, argv, cwd=cwd) if target == "linux" else list(argv)
    if target not in ("host", "linux"):
        raise ValueError("Unknown execution target")
    native = target == "host" or config.get("provider") == "native"
    return subprocess.run(actual, cwd=cwd if native else None, capture_output=True,
                          text=True, errors="replace", timeout=timeout)
