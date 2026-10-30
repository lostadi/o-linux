"""Route saved-drive commands in a provisioned Linux guest to their Mac owner."""
import json
import os
from pathlib import Path, PurePosixPath
import sys

from . import guest_mac


def host_configuration():
    """Use the provisioned, pinned Mac bridge, never a command found on PATH."""
    path = Path(os.environ.get("O_LINUX_HOST_CONFIG", "/etc/o-linux/host.json"))
    if not path.exists():
        raise RuntimeError("The saved-drive reader requires macOS and anylinuxfs; "
                           "no Mac bridge is provisioned on this native Linux host "
                           "(missing " + str(path) + ")")
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("expected a regular host configuration file")
        info = path.stat()
        if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            raise ValueError("host configuration must belong to root or this user and not be group/world writable")
        config = json.loads(path.read_text())
        if not isinstance(config, dict) or config.get("host_alias") != "o-linux-mac":
            raise ValueError("expected the provisioned o-linux-mac host bridge")
        # The installer has always declared this pinned alias for the Mac host.
        # Respect an explicit platform if a newer installation supplies one.
        if config.get("host_platform", "Darwin") != "Darwin":
            raise ValueError("the saved-drive bridge target must be macOS")
        for name in ("host_home", "host_user", "host_address", "identity", "known_hosts"):
            value = config.get(name)
            if not isinstance(value, str) or not value or "\0" in value:
                raise ValueError("missing or invalid " + name)
        for name in ("host_home", "identity", "known_hosts"):
            value = PurePosixPath(config[name])
            if not value.is_absolute() or ".." in value.parts or str(value) == "/":
                raise ValueError(name + " must be an absolute path without parent traversal")
        return config
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError("Invalid Mac drive bridge configuration " + str(path) + ": " + str(error)) from error


def forward(arguments):
    config = host_configuration()
    command = str(PurePosixPath(config["host_home"]) / ".local/bin/o-linux")
    # Keep captured JSON and separate streams intact. An interactive terminal
    # permits the host's existing sudo flow for open/close without a new policy.
    tty = all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))
    options = ["--tty"] if tty else []
    # The internal host entry refuses non-macOS destinations before loading
    # configuration, so even a misconfigured bridge cannot forward recursively.
    return guest_mac.main([*options, "--", command, "_host-drive", *arguments])
