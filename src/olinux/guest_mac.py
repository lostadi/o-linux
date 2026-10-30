#!/usr/bin/env python3
"""Call a native Mac command from Linux without changing its arguments."""
import json
import os
from pathlib import Path
import shlex
import sys

USAGE = """Usage: mac [--tty] [--cwd HOST_DIRECTORY] [--] COMMAND [ARG ...]

Run a native Mac command from Linux. Arguments, streams and exit status are
preserved. Use --tty when the command needs an interactive host terminal.
Shared working directories are retained; other paths use the Mac home.
This uses the host account's existing permissions and does not mount disks.
"""


def parse_arguments(args):
    args = list(args)
    cwd, tty = None, False
    while args:
        if args[0] == "--tty":
            tty, args = True, args[1:]
        elif args[0] == "--cwd":
            if len(args) < 3 or not os.path.isabs(args[1]):
                raise ValueError("--cwd needs an absolute host directory and a command")
            cwd, args = args[1], args[2:]
        elif args[0] == "--":
            args = args[1:]
            break
        else:
            break
    if not args:
        raise ValueError("a command is required")
    return args, cwd, tty


def shared_cwd(current, config):
    roots = [config["host_home"]]
    if config.get("share_volumes", True):
        roots.append("/Volumes")
    return current if any(current == root or current.startswith(root + "/") for root in roots) else config["host_home"]


def build_ssh(argv, cwd, config, tty=False):
    host = os.environ.get("O_LINUX_HOST", config["host_address"])
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        raise ValueError("invalid host address")
    request = json.dumps({"version": 1, "argv": argv, "cwd": cwd},
                         ensure_ascii=True, separators=(",", ":"))
    return ["/usr/bin/ssh", "-F", "/dev/null", "-tt" if tty else "-T",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + config["known_hosts"],
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "HostKeyAlias=" + config.get("host_alias", "o-linux-mac"),
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", "-o", "ClearAllForwardings=yes",
            "-i", config["identity"], "-l", config["host_user"], "--", host,
            shlex.quote(request)]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0 if args else 64
    try:
        config_file = Path(os.environ.get("O_LINUX_HOST_CONFIG", "/etc/o-linux/host.json"))
        config = json.loads(config_file.read_text())
        command, cwd, tty = parse_arguments(args)
        if cwd is None:
            try:
                cwd = shared_cwd(os.getcwd(), config)
            except OSError:
                cwd = config["host_home"]
        for key in ("identity", "known_hosts"):
            path = Path(config[key])
            if path.is_symlink() or not path.is_file():
                raise ValueError("missing regular connection file: " + str(path))
        if Path(config["identity"]).stat().st_mode & 0o077:
            raise ValueError("private key permissions must be 0600")
        os.execv("/usr/bin/ssh", build_ssh(command, cwd, config, tty))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("mac: " + str(error), file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
