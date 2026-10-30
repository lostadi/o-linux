"""Run the installed foreign-system harness in Linux through an OSTADIX job."""
from pathlib import Path, PurePosixPath

from .config import load_config, state_dir
from .ostadix import environment, job


def _has_option(arguments, name):
    return any(value == name or value.startswith(name + "=") for value in arguments)


def _subcommand(arguments):
    """Find the harness command without mistaking a global option's value for it."""
    skip = False
    for argument in arguments:
        if skip:
            skip = False
        elif argument in ("--manifest", "--guest-dir"):
            skip = True
        elif not argument.startswith("-"):
            return argument
    return None


def _shared(path, config):
    path = Path(path).expanduser().resolve()
    roots = [Path(config["host_home"]).expanduser().resolve()]
    if config.get("share_volumes", True):
        roots.append(Path("/Volumes"))
    return any(path == root or root in path.parents for root in roots)


def _linux_root(arguments, configured=None):
    """Consume our placement option without resolving a guest path on the host."""
    remaining = []
    selected = configured
    seen = False
    iterator = iter(arguments)
    for argument in iterator:
        if argument == "--linux-root" or argument.startswith("--linux-root="):
            if seen:
                raise ValueError("Specify --linux-root only once")
            seen = True
            if argument == "--linux-root":
                selected = next(iterator, None)
                if selected is None:
                    raise ValueError("--linux-root requires an absolute Linux path")
            else:
                selected = argument.split("=", 1)[1]
        else:
            remaining.append(argument)
    if selected is None:
        return None, remaining
    if not isinstance(selected, str) or not selected or any(c in selected for c in "\0\n\r"):
        raise ValueError("linux_ostadix_root must be a nonempty absolute Linux path")
    path = PurePosixPath(selected)
    if not path.is_absolute() or selected.startswith("//") or ".." in path.parts or str(path) != selected:
        raise ValueError("linux_ostadix_root must be an absolute normalized Linux path without . or .. components")
    return path, remaining


def run_linux(arguments):
    config = load_config()
    root, _, _ = environment(config)
    provider = config.get("provider", "multipass")
    if provider not in ("multipass", "native"):
        raise RuntimeError("Unsupported Linux execution provider: " + str(provider))
    linux_root, arguments = _linux_root(arguments, config.get("linux_ostadix_root"))
    harness = (linux_root or root) / "scripts/foreign_kernel_lab.py"
    # An explicit guest-native checkout is not a path in the Mac filesystem.
    # Its existence and provenance are established by the dispatched harness.
    if (linux_root is None or provider == "native") and not Path(harness).is_file():
        raise RuntimeError("The configured OSTADIX checkout has no foreign-system harness: " + str(harness))
    if linux_root is None and provider == "multipass" and not _shared(root, config):
        raise RuntimeError("The OSTADIX checkout must be inside a shared host home or enabled /Volumes path for systems --linux")
    argv = list(arguments) or ["list"]
    if not _has_option(argv, "--guest-dir"):
        cache = Path(config["host_home"]) / ".local/share/ostadix/guests"
        argv = ["--guest-dir", str(cache), *argv]
    if _subcommand(argv) in ("run", "run-all") and not _has_option(argv, "--output-dir"):
        output = state_dir() / "foreign-system-runs"
        if provider == "multipass" and not _shared(output, config):
            raise RuntimeError("The default system-run output is outside the guest shares; use --output-dir with a Linux-accessible path")
        argv += ["--output-dir", str(output)]
    return job(["--target", "linux", "--", "python3", str(harness), *argv])
