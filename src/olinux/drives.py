"""Save one external ext4 partition and open it through a verified read-only mount."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

from .config import command, load_config, run, state_dir

DEFAULT_MOUNT = "/Volumes/Linux-Drive-ReadOnly"
AUDITED_ANYLINUXFS_VERSION = "0.20.1"
PARTITION = re.compile(r"/dev/disk[0-9]+s[0-9]+")


def canonical_uuid(value, label="UUID"):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError):
        raise RuntimeError(f"Missing or invalid {label}") from None


def mount_path(value):
    path = Path(value)
    if (not path.is_absolute() or path.parent != Path("/Volumes")
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]*", path.name)):
        raise RuntimeError("Use a named mount directory directly under /Volumes")
    return str(path)


def disk_info(device):
    return plistlib.loads(run(["/usr/sbin/diskutil", "info", "-plist", device]).stdout.encode())


def inventory():
    result = plistlib.loads(run(["/usr/sbin/diskutil", "list", "-plist", "external"]).stdout.encode())
    names = set(result.get("AllDisks", []))
    def walk(value):
        if isinstance(value, dict):
            if value.get("DeviceIdentifier"):
                names.add(value["DeviceIdentifier"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(result.get("AllDisksAndPartitions", []))
    return sorted("/dev/" + name for name in names if re.fullmatch(r"disk[0-9]+s[0-9]+", name))


def selection_for(device, mountpoint=DEFAULT_MOUNT):
    """Use diskutil metadata only; selection neither probes nor mounts a source."""
    if not PARTITION.fullmatch(device):
        raise RuntimeError("Select an exact partition path such as /dev/disk4s3")
    partition = disk_info(device)
    parent = partition.get("ParentWholeDisk", "")
    if (partition.get("DeviceNode") != device or partition.get("WholeDisk") is not False
            or partition.get("Internal") is not False or not re.fullmatch(r"disk[0-9]+", parent)):
        raise RuntimeError("Expected a partition on an external whole disk")
    whole = disk_info("/dev/" + parent)
    if (whole.get("WholeDisk") is not True or whole.get("Internal") is not False
            or whole.get("VirtualOrPhysical") != "Physical"
            or whole.get("DeviceNode") != "/dev/" + parent):
        raise RuntimeError("Selected drive must be external and physical")
    partition_bytes, whole_bytes = partition.get("TotalSize"), whole.get("TotalSize")
    if (type(partition_bytes) is not int or type(whole_bytes) is not int
            or not 0 < partition_bytes <= whole_bytes or not whole.get("MediaName")):
        raise RuntimeError("Incomplete drive size or media identity")
    fs_uuid = partition.get("VolumeUUID") or partition.get("FileSystemUUID")
    return {"partition_uuid": canonical_uuid(partition.get("DiskUUID"), "partition UUID"),
            "filesystem_uuid": canonical_uuid(fs_uuid, "filesystem UUID") if fs_uuid else None,
            "filesystem": "ext4", "partition_bytes": partition_bytes, "whole_bytes": whole_bytes,
            "media_name": whole["MediaName"], "bus_protocol": whole.get("BusProtocol"),
            "mountpoint": mount_path(mountpoint)}


def locate(selection):
    matches = []
    for device in inventory():
        device = "/dev/" + Path(device).name
        if not PARTITION.fullmatch(device):
            continue
        info = disk_info(device)
        value = info.get("DiskUUID")
        if value and canonical_uuid(value, "partition UUID") == selection["partition_uuid"]:
            matches.append(device)
    if len(matches) != 1:
        raise RuntimeError("Saved partition is not connected" if not matches
                           else "Duplicate partition UUID; refusing an ambiguous drive")
    device = matches[0]
    current = selection_for(device, selection["mountpoint"])
    for key in ("partition_uuid", "partition_bytes", "whole_bytes", "media_name", "bus_protocol"):
        if current[key] != selection[key]:
            raise RuntimeError(f"Saved drive identity changed: {key}; no action taken")
    if (selection.get("filesystem_uuid") and current.get("filesystem_uuid")
            and selection["filesystem_uuid"] != current["filesystem_uuid"]):
        raise RuntimeError("Saved filesystem UUID changed; no action taken")
    return device


def load_record():
    path = state_dir() / "drives.json"
    if path.is_symlink():
        raise RuntimeError("Refusing symlinked saved-drive state")
    if not path.exists():
        raise RuntimeError("No drive selected; run linux drive select /dev/diskNsN first")
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or not isinstance(record.get("selection"), dict):
        raise RuntimeError("Malformed saved-drive state")
    selection = record.get("selection", {})
    if record.get("version") != 1 or selection.get("filesystem") != "ext4":
        raise RuntimeError("Unsupported saved-drive state")
    selection["partition_uuid"] = canonical_uuid(selection.get("partition_uuid"), "partition UUID")
    if selection.get("filesystem_uuid"):
        selection["filesystem_uuid"] = canonical_uuid(selection["filesystem_uuid"], "filesystem UUID")
    mount_path(selection.get("mountpoint", ""))
    for key in ("partition_bytes", "whole_bytes"):
        if type(selection.get(key)) is not int or selection[key] <= 0:
            raise RuntimeError("Invalid saved drive size")
    if not selection.get("media_name"):
        raise RuntimeError("Missing saved media identity")
    return record


def save_record(record):
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "drives.json"
    if path.is_symlink():
        raise RuntimeError("Refusing symlinked saved-drive state")
    fd, name = tempfile.mkstemp(prefix=".drives.", dir=root)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(record, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def drive_lock():
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / "drives.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another saved-drive operation is running") from None
        yield


def require_sudo():
    if run(["/usr/bin/sudo", "-n", "/usr/bin/true"], check=False).returncode:
        raise RuntimeError("Run sudo -v in your terminal, then retry this command")


def any_status(app):
    result = run([app, "status"])
    if getattr(result, "stderr", "").strip():
        raise RuntimeError("anylinuxfs reports an incomplete mount: " + result.stderr.strip())
    return result.stdout.strip()


def pty_text(argv, timeout=30, max_output=1024 * 1024):
    """Run a bounded metadata command with the controlling TTY anylinuxfs needs."""
    import errno
    import pty
    import select
    import signal
    import struct
    import termios
    import time

    pid, master = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
            os.execvpe(argv[0], argv, dict(os.environ, TERM="dumb"))
        except BaseException as error:
            os.write(2, (str(error) + "\n").encode())
            os._exit(127)
    output = bytearray()
    status = None
    eof = False
    completed = False
    deadline = time.monotonic() + timeout
    try:
        while not (eof and status is not None):
            if status is None:
                waited, value = os.waitpid(pid, os.WNOHANG)
                if waited:
                    status = value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Guest metadata command timed out after {timeout:g} seconds")
            if eof:
                time.sleep(min(0.05, remaining))
                continue
            ready, _, _ = select.select([master], [], [], min(0.1, remaining))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    eof = True
                else:
                    output.extend(chunk)
                    if len(output) > max_output:
                        raise RuntimeError("Guest metadata output exceeded its size limit")
        completed = True
    finally:
        os.close(master)
        if not completed:
            # The forked child owns this new session and process group.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if status is not None:
                    break
                try:
                    os.killpg(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                until = time.monotonic() + 1
                while status is None and time.monotonic() < until:
                    waited, value = os.waitpid(pid, os.WNOHANG)
                    if waited:
                        status = value
                        break
                    time.sleep(0.05)
            if status is None:
                raise RuntimeError(f"PTY command stopped responding; cleanup of owned PID {pid} is unverified")
    text = output.decode("utf-8", errors="replace")
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "").strip()
    code = os.waitstatus_to_exitcode(status)
    if code:
        raise RuntimeError(f"Guest metadata command failed ({code}): {text}")
    return text


GUEST_PTY_HELPER = """import sys
sys.path.insert(0, sys.argv[1])
from olinux.drives import pty_text
try:
    print(pty_text(sys.argv[2:], timeout=30))
except Exception as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
"""


def guest_text(app, mountpoint, args):
    args = list(args)
    # The VM's exec service does not include /sbin in its command search path.
    args[0] = {"cat": "/bin/cat", "blkid": "/sbin/blkid"}.get(args[0], args[0])
    # Authenticate in the caller's terminal context before making a new PTY.
    package_root = str(Path(__file__).resolve().parent.parent)
    result = run(["/usr/bin/sudo", "-n", sys.executable, "-I", "-S", "-c", GUEST_PTY_HELPER,
                  package_root, app, "vm", "exec", mountpoint, "--", *args], timeout=35)
    return result.stdout.strip()


def mount_id(path):
    return os.stat(path).st_dev


def unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def parse_status(status):
    entries = []
    for line in status.splitlines():
        match = re.fullmatch(r"(.+?) on (/.*?) \(([^()]*)\)(?: VM\[.*\])?", line.strip())
        if not match:
            raise RuntimeError("Unrecognized anylinuxfs status; no action taken")
        entries.append({"device": match[1], "mountpoint": match[2],
                        "options": {value.strip() for value in match[3].split(",")}, "line": match[0]})
    return entries


def selected_mount(entries, device, mountpoint):
    matches = []
    for entry in entries:
        same_device = entry["device"] in (device, Path(device).name)
        same_path = entry["mountpoint"] == mountpoint
        if same_device != same_path:
            raise RuntimeError("Selected device or destination conflicts with an existing anylinuxfs mount")
        if same_device:
            matches.append(entry)
    if len(matches) > 1:
        raise RuntimeError("Ambiguous anylinuxfs mount identity")
    return matches[0] if matches else None


def status_entry(status, device, mountpoint):
    entry = selected_mount(parse_status(status), device, mountpoint)
    if not entry:
        raise RuntimeError("Expected saved drive mount was not found in anylinuxfs status")
    options = entry["options"]
    if not {"ext4", "ro", "noload"} <= options or "rw" in options:
        raise RuntimeError("anylinuxfs did not confirm ext4 ro,noload")
    return entry["line"]


def host_mount(mountpoint):
    found = []
    suffix = " on " + mountpoint
    for line in run(["/sbin/mount"]).stdout.splitlines():
        match = re.fullmatch(r"(.+) \(([^()]*)\)", line)
        body = unescape_mount(match[1]) if match else ""
        if match and body.endswith(suffix):
            found.append((body[:-len(suffix)], {value.strip() for value in match[2].split(",")}))
    if len(found) != 1:
        raise RuntimeError("Expected host mount was not uniquely verified")
    source, options = found[0]
    if not {"nfs", "read-only"} <= options or "rw" in options:
        raise RuntimeError("Host NFS read-only flag was not verified")
    return source


def verify_mount(app, selection, device, status_text=None):
    """Verify each read-only layer without listing files or testing source writes."""
    mountpoint = selection["mountpoint"]
    status = any_status(app) if status_text is None else status_text
    status_line = status_entry(status, device, mountpoint)
    if Path(mountpoint).is_symlink() or not os.path.ismount(mountpoint):
        raise RuntimeError("Expected a real host mount, not a symlink or directory")
    host_source = host_mount(mountpoint)
    guest_path = "/mnt/" + Path(mountpoint).name
    if host_source != Path(device).name + ".local:" + guest_path:
        raise RuntimeError("Host NFS source does not match the selected drive")
    source_uuid = signature(device, selection.get("filesystem_uuid"))
    mounts = guest_text(app, mountpoint, ["cat", "/proc/mounts"])
    rows = [row.split() for row in mounts.splitlines()]
    rows = [row for row in rows if len(row) == 6 and unescape_mount(row[1]) == guest_path]
    if len(rows) != 1:
        raise RuntimeError("Guest source mount was not uniquely verified")
    source, _, filesystem, options, _, _ = rows[0]
    options = set(options.split(","))
    if (filesystem != "ext4" or "ro" not in options or "rw" in options
            or not options.intersection({"noload", "norecovery"})
            or not re.fullmatch(r"/dev/vd[a-z]+", source)):
        raise RuntimeError("Guest ext4 read-only/no-journal-replay flags were not verified")
    if guest_text(app, mountpoint, ["cat", "/sys/class/block/" + Path(source).name + "/ro"]) != "1":
        raise RuntimeError("Guest block device is not read-only")
    exports = guest_text(app, mountpoint, ["cat", "/etc/exports"])
    matching = []
    for line in exports.splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0] == guest_path:
            matching.append(words[1:])
    if len(matching) != 1 or not matching[0]:
        raise RuntimeError("Guest NFS export was not uniquely verified")
    for client in matching[0]:
        match = re.fullmatch(r"[^()]+\(([^()]*)\)", client)
        flags = set(match[1].split(",")) if match else set()
        if "ro" not in flags or "rw" in flags:
            raise RuntimeError("Guest NFS export is not read-only")
    probe = guest_text(app, mountpoint, ["blkid", "-p", "-o", "export", source])
    values = dict(line.split("=", 1) for line in probe.splitlines() if "=" in line)
    filesystem_uuid = canonical_uuid(values.get("UUID"), "guest filesystem UUID")
    if (values.get("TYPE") != "ext4" or filesystem_uuid != source_uuid
            or (selection.get("filesystem_uuid") and filesystem_uuid != selection["filesystem_uuid"])):
        raise RuntimeError("Guest filesystem identity does not match the saved drive")
    return {"mounted": True, "read_only": True, "device": device, "mountpoint": mountpoint,
            "partition_uuid": selection["partition_uuid"], "filesystem": "ext4",
            "filesystem_uuid": filesystem_uuid, "host_source": host_source,
            "guest_source": source, "host_mount_id": mount_id(mountpoint),
            "anylinuxfs_status": status_line}


def signature(device, expected_uuid):
    value = run(["/usr/bin/sudo", "-n", "/usr/bin/file", "-s", "/dev/r" + Path(device).name]).stdout
    match = re.search(r"\bUUID=([0-9a-fA-F-]{36})\b", value)
    if "ext4 filesystem" not in value or not match:
        raise RuntimeError("Selected partition did not identify as an ext4 filesystem")
    result = canonical_uuid(match[1], "filesystem UUID")
    if expected_uuid and result != expected_uuid:
        raise RuntimeError("Filesystem signature UUID changed; no mount attempted")
    return result


def operate(args):
    if args.action == "select":
        if (state_dir() / "drives.json").exists() and load_record().get("mount_attempt"):
            raise RuntimeError("A prior mount is pending; reconcile with open/status before changing selection")
        selection = selection_for(args.device, args.mountpoint)
        record = {"version": 1, "selection": selection, "mount": None}
        save_record(record)
        return {"selected": True, "mount_performed": False, "selection": selection}
    record = load_record()
    selection = record["selection"]
    device = locate(selection)
    app = command("anylinuxfs", load_config())
    status = any_status(app)
    mountpoint = selection["mountpoint"]
    active = selected_mount(parse_status(status), device, mountpoint)
    if args.action == "status" and not active and not os.path.ismount(mountpoint):
        return {"selected": True, "connected": True, "mounted": False, "device": device,
                "selection": selection, "mount_pending": bool(record.get("mount_attempt"))}
    require_sudo()
    if args.action == "open" and not active:
        if record.get("mount_attempt"):
            raise RuntimeError("Prior mount completion is unknown; retaining pending state without another mount attempt")
        if os.path.ismount(mountpoint) or disk_info(device).get("MountPoint"):
            raise RuntimeError("Selected partition or destination is already mounted elsewhere")
        target = Path(mountpoint)
        if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
            raise RuntimeError("Mount directory must be an empty real directory")
        fs_uuid = signature(device, selection.get("filesystem_uuid"))
        if locate(selection) != device or selected_mount(parse_status(any_status(app)), device, mountpoint):
            raise RuntimeError("Drive identity or active mount changed; no mount attempted")
        selection["filesystem_uuid"] = fs_uuid
        run(["/usr/bin/sudo", "-n", "/bin/mkdir", "-p", mountpoint])
        record["mount_attempt"] = {"device": device, "partition_uuid": selection["partition_uuid"],
                                   "operation_id": uuid.uuid4().hex}
        save_record(record)
        try:
            run(["/usr/bin/sudo", "-n", app, Path(device).name, mountpoint, "-t", "ext4", "-o", "ro,noload",
                 "-n", "ro", "--ignore-permissions", "--net-helper", "gvproxy", "-b", "127.0.0.1",
                 "-w", "false"], timeout=120)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"Mount completion is uncertain: {error}. Pending state retained; "
                               "use linux drive status/open to reconcile") from error
        status = any_status(app)
    # An unrelated active instance is never stopped or remounted to make room.
    verified = verify_mount(app, selection, device, status)
    if locate(selection) != device:
        raise RuntimeError("Drive identity changed during mount verification")
    version = run([app, "--version"]).stdout.strip()
    verified["anylinuxfs_version"] = version
    verified["source_audit_version"] = AUDITED_ANYLINUXFS_VERSION
    verified["source_audit_version_matches"] = version.split()[-1:] == [AUDITED_ANYLINUXFS_VERSION]
    if args.action == "status":
        return verified
    if args.action == "close":
        owned = record.get("mount")
        keys = ("partition_uuid", "filesystem_uuid", "device", "mountpoint", "host_source", "host_mount_id")
        if not owned or any(owned.get(key) != verified.get(key) for key in keys):
            raise RuntimeError("No ownership receipt for this exact mount; leaving it unchanged")
        run(["/usr/bin/sudo", "-n", app, "unmount", mountpoint, "--wait-for-vm=20"], timeout=40)
        if os.path.ismount(mountpoint) or selected_mount(parse_status(any_status(app)), device, mountpoint):
            raise RuntimeError("Unmount completion is uncertain; retaining the ownership receipt")
        record["mount"] = None
        record.pop("mount_attempt", None)
        save_record(record)
        return {"closed": True, "partition_uuid": selection["partition_uuid"], "mountpoint": mountpoint}
    selection["filesystem_uuid"] = verified["filesystem_uuid"]
    record["mount"] = verified
    record.pop("mount_attempt", None)
    save_record(record)
    return verified


def dispatch(argv=None):
    parser = argparse.ArgumentParser(prog="linux drive", description=__doc__)
    parser.add_argument("action", nargs="?", choices=("select", "open", "status", "close"), default="open")
    parser.add_argument("device", nargs="?", help="Exact /dev/diskNsN partition, for select only")
    parser.add_argument("--mountpoint", default=DEFAULT_MOUNT, help="Saved mount directory under /Volumes")
    parser.add_argument("--filesystem", choices=("ext4",), default="ext4")
    args = parser.parse_args(argv)
    if (args.action == "select") != bool(args.device):
        parser.error("select requires a partition; open/status/close use the saved selection")
    if platform.system() != "Darwin":
        raise RuntimeError("The saved-drive reader currently requires macOS and anylinuxfs")
    with drive_lock():
        return operate(args)


def main(argv=None):
    try:
        print(json.dumps(dispatch(argv), indent=2))
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Drive access failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
