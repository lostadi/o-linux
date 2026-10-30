#!/usr/bin/env python3
"""Run an authenticated request with the native host account's permissions."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys

MAX_REQUEST_BYTES = 262144


def parse_request(original, home):
    if not original or len(original.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("missing or oversized command request")
    words = shlex.split(original, posix=True)
    if len(words) != 1:
        raise ValueError("expected one quoted JSON request")
    request = json.loads(words[0])
    if not isinstance(request, dict) or request.get("version") != 1:
        raise ValueError("unsupported request version")
    if set(request) - {"version", "argv", "cwd"}:
        raise ValueError("unknown request field")
    argv = request.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ValueError("argv must be a nonempty array")
    if any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]:
        raise ValueError("arguments must be strings without NUL bytes")
    cwd = request.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or "\0" in cwd):
        raise ValueError("cwd must be a string without NUL bytes")
    if not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        cwd = home
    return argv, cwd


def native_environment(config, cwd):
    home = config["host_home"]
    directories = [str(Path(home) / ".local/bin"), "/opt/homebrew/bin",
                   "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin",
                   "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    environment = os.environ.copy()
    environment.update(PATH=":".join(directories), HOME=home, PWD=cwd)
    for key in ("SSH_ORIGINAL_COMMAND", "PYTHONPATH", "PYTHONHOME"):
        environment.pop(key, None)
    return environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text())
        home = config["host_home"]
        if not isinstance(home, str) or not os.path.isabs(home):
            raise ValueError("invalid host home configuration")
        command, cwd = parse_request(os.environ.get("SSH_ORIGINAL_COMMAND", ""), home)
        os.chdir(cwd)
        os.execvpe(command[0], command, native_environment(config, cwd))
    except FileNotFoundError as error:
        print("mac host bridge: " + str(error), file=sys.stderr)
        return 127
    except (ValueError, KeyError, TypeError) as error:
        print("mac host bridge: " + str(error), file=sys.stderr)
        return 64
    except OSError as error:
        print("mac host bridge: " + str(error), file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
