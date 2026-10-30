"""One terminal interface for Linux, native host access and OSTADIX systems."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from .config import command, load_config, run, vm_state
from .providers import ensure_running, linux_argv

HELP = """o-linux — persistent Linux and OSTADIX system workflows

o-linux                        Enter Linux; start its VM when needed
o-linux exec COMMAND [ARGS...]  Run a literal Linux command
o-linux job [--plan] [--target linux|host] -- COMMAND [ARGS...]
                               Plan and execute a recorded Ostadix document
o-linux ostadix [ARGS...]       Use the configured native o command
o-linux kernel [ARGS...]        Build/boot/check OKernel through o's kernel tool
o-linux systems [ARGS...]       Use OSTADIX's pinned foreign-kernel laboratory
o-linux systems --linux [ARGS...] Run that laboratory inside Linux through O
o-linux inventory [CATEGORY]    Native host peripheral metadata
o-linux disks                  Host external-disk metadata
o-linux drive [open|status|select DEVICE|close]
                               Remember and open a drive through O in its saved mode
o-linux drive mode rw|ro        Save the access mode while the selected drive is closed
o-linux drive select DEVICE [--read-write|--read-only]
                               New selections use read-write unless explicitly read-only
o-linux mac [ARGS...]           Native Mac command from the Linux guest
o-linux image IMAGE.raw [--read-write|--read-only]
                               Image bridge using the configured access mode
o-linux disk DEVICE --expected-size BYTES
                               Physical-disk bridge; same access-mode flags
o-linux status | start | stop | doctor

The linux shortcut uses the same interface. Direct commands remain available:
linux python3 --version
"""


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["--help"], ["-h"], ["help"]):
        print(HELP); return 0
    if args[:1] == ["--version"]:
        from . import __version__
        print(__version__); return 0
    if args[:1] == ["_host-drive"]:
        if platform.system() != "Darwin":
            raise RuntimeError("Host drive requests must execute on macOS; refusing to forward again")
        args = ["drive", *args[1:]]
    if args[:1] == ["job"]:
        from .ostadix import job
        return job(args[1:])
    if args[:1] == ["inventory"]:
        from .peripherals import inventory
        print(json.dumps(inventory(args[1] if len(args) > 1 else "all"), indent=2)); return 0
    config = load_config()
    provider = config.get("provider", "multipass")
    if args[:1] == ["drive"]:
        if provider == "native" and platform.system() == "Linux":
            from .guest_drive import forward
            return forward(args[1:])
        if len(args) == 1 or args[1:] == ["open"]:
            from .drive_frontend import run_open
            print(json.dumps(run_open(), indent=2)); return 0
        from .drives import main as drives_main
        return drives_main(args[1:])
    if args[:2] == ["systems", "--linux"]:
        from .systems import run_linux
        return run_linux(args[2:])
    if args[:1] in (["ostadix"], ["kernel"], ["systems"]):
        from .ostadix import native_argv
        actual, env = native_argv(config, args[0], args[1:])
        os.execvpe(actual[0], actual, env)
    if args[:1] == ["status"]:
        if provider == "native": print(json.dumps(dict(provider="native", platform=platform.platform()), indent=2))
        else: print(run([command("multipass", config), "info", config["instance"], "--format", "json"]).stdout)
        return 0
    if args[:1] == ["stop"]:
        if provider == "native": print("Native Linux has no managed VM to stop."); return 0
        return subprocess.call([command("multipass", config), "stop", config["instance"]])
    if args[:1] == ["doctor"]:
        tools = {n: shutil.which(n) for n in ("ssh", "O", "o", "qemu-nbd", "qemu-system-x86_64", "ddrescue", "smartctl")}
        print(json.dumps(dict(provider=provider, vm=vm_state(config) if provider != "native" else None, tools=tools), indent=2)); return 0
    if args[:1] == ["disks"]:
        from .peripherals import inventory
        print(json.dumps(inventory("disks"), indent=2)); return 0
    if args[:1] in (["image"], ["disk"]):
        if provider == "native": raise RuntimeError("This NBD-to-Multipass bridge is for macOS; native Linux can access its own block devices directly")
        ensure_running(config)
        from .storage import main as storage_main
        return storage_main(args[1:] if args[0] == "image" else ["--device", *args[1:]])
    address = ensure_running(config)
    if args[:1] == ["start"]:
        print("Linux ready" + (" at " + address if address else " (native)")); return 0
    if args[:1] == ["exec"]: args = args[1:]
    if not args or args == ["shell"]:
        args = [os.environ.get("SHELL", "/bin/bash"), "-l"] if provider == "native" else ["/bin/bash", "-l"]
    if args[:1] == ["mac"] and provider == "native" and not shutil.which("mac"):
        raise RuntimeError("No Mac bridge is provisioned on this native Linux host")
    tty = all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))
    actual = linux_argv(config, args, tty=tty, address=address)
    os.execvp(actual[0], actual)


def entry():
    try:
        return main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print("o-linux: " + str(error), file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(entry())
