"""Save one external ext4 partition and open it with a verified access mode."""

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
import stat
import subprocess
import sys
import tempfile
import uuid

from .config import command, load_config, run, state_dir

DEFAULT_MOUNT = "/Volumes/Linux-Drive"
AUDITED_ANYLINUXFS_VERSION = "0.20.1"
PARTITION = re.compile(r"/dev/disk[0-9]+s[0-9]+")


def access_mode(selection):
    """Old profiles remain read-only until their mode is explicitly changed."""
    mode = selection.get("access_mode", "ro")
    if mode not in ("ro", "rw"):
        raise RuntimeError("Saved drive access_mode must be ro or rw")
    return mode


def receipt_mode(receipt):
    mode = access_mode(receipt)
    if receipt.get("read_only") is not (mode == "ro"):
        raise RuntimeError("Mount receipt access mode is inconsistent")
    return mode


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


def selection_for(device, mountpoint=DEFAULT_MOUNT, mode="rw"):
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
            "mountpoint": mount_path(mountpoint), "access_mode": access_mode({"access_mode": mode})}


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
    access_mode(selection)
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
    probe = ["/usr/bin/sudo", "-n", "/usr/bin/true"]
    if not run(probe, check=False).returncode:
        return
    if all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        # Keep authentication and subsequent sudo calls in this same host PTY.
        if not subprocess.call(["/usr/bin/sudo", "-v"]) and not run(probe, check=False).returncode:
            return
    raise RuntimeError("macOS authorization required; run o-linux drive in an interactive terminal "
                       "and authenticate there (sudo -v runs in that same host session)")


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


def check_mount_options(options, mode):
    if mode == "ro":
        valid = {"ext4", "ro", "noload"} <= options and "rw" not in options
    else:
        valid = {"ext4", "rw"} <= options and not options.intersection({"ro", "noload", "norecovery"})
    if not valid:
        raise RuntimeError("anylinuxfs did not confirm ext4 " + ("ro,noload" if mode == "ro" else "rw with journal replay enabled"))


def status_entry(status, device, mountpoint, mode="ro"):
    entry = selected_mount(parse_status(status), device, mountpoint)
    if not entry:
        raise RuntimeError("Expected saved drive mount was not found in anylinuxfs status")
    check_mount_options(entry["options"], mode)
    return entry["line"]


def host_mount(mountpoint, mode="ro"):
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
    if ("nfs" not in options or (mode == "ro" and ("read-only" not in options or "rw" in options))
            or (mode == "rw" and options.intersection({"read-only", "ro"}))):
        raise RuntimeError("Host NFS " + mode + " mode was not verified")
    return source


def verify_mount(app, selection, device, status_text=None):
    """Verify each access-mode layer without listing files or testing writes."""
    mountpoint = selection["mountpoint"]
    mode = access_mode(selection)
    status = any_status(app) if status_text is None else status_text
    status_line = status_entry(status, device, mountpoint, mode)
    if Path(mountpoint).is_symlink() or not os.path.ismount(mountpoint):
        raise RuntimeError("Expected a real host mount, not a symlink or directory")
    host_source = host_mount(mountpoint, mode)
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
    if (filesystem != "ext4" or mode not in options or ("rw" if mode == "ro" else "ro") in options
            or (mode == "ro" and not options.intersection({"noload", "norecovery"}))
            or (mode == "rw" and options.intersection({"noload", "norecovery"}))
            or not re.fullmatch(r"/dev/vd[a-z]+", source)):
        raise RuntimeError("Guest ext4 " + mode + " mount flags were not verified")
    block_read_only = guest_text(app, mountpoint, ["cat", "/sys/class/block/" + Path(source).name + "/ro"])
    if block_read_only != ("1" if mode == "ro" else "0"):
        raise RuntimeError("Guest block device does not match " + mode + " mode")
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
        if mode not in flags or ("rw" if mode == "ro" else "ro") in flags:
            raise RuntimeError("Guest NFS export does not match " + mode + " mode")
    probe = guest_text(app, mountpoint, ["blkid", "-p", "-o", "export", source])
    values = dict(line.split("=", 1) for line in probe.splitlines() if "=" in line)
    filesystem_uuid = canonical_uuid(values.get("UUID"), "guest filesystem UUID")
    if (values.get("TYPE") != "ext4" or filesystem_uuid != source_uuid
            or (selection.get("filesystem_uuid") and filesystem_uuid != selection["filesystem_uuid"])):
        raise RuntimeError("Guest filesystem identity does not match the saved drive")
    return {"mounted": True, "read_only": mode == "ro", "access_mode": mode,
            "device": device, "mountpoint": mountpoint,
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


OWNED_MOUNT_KEYS = ("partition_uuid", "filesystem_uuid", "device", "mountpoint", "host_source", "host_mount_id")


def registry_media(document):
    """Deduplicate ioreg's overlapping roots while retaining media ancestry."""
    if not isinstance(document, list):
        raise RuntimeError("Unrecognized IOMedia registry metadata")
    fields = ("BSD Name", "BSD Major", "BSD Minor", "IOObjectClass", "IOServiceState", "Size", "Whole", "UUID")
    media = {}
    pending = [(node, None) for node in document]
    while pending:
        node, parent = pending.pop()
        if not isinstance(node, dict):
            raise RuntimeError("Unrecognized IOMedia registry node")
        if node.get("IOObjectClass") == "IOMedia" or "BSD Name" in node:
            identity = node.get("IORegistryEntryID")
            if type(identity) is not int or identity <= 0:
                raise RuntimeError("Missing IOMedia registry identity")
            values = {key: node.get(key) for key in fields}
            previous = media.get(identity)
            if previous and any(type(previous["values"][key]) is not type(values[key])
                                or previous["values"][key] != values[key] for key in fields):
                raise RuntimeError("Conflicting copies of an IOMedia registry entry")
            entry = media.setdefault(identity, {"values": values, "parents": set()})
            if parent is not None:
                entry["parents"].add(parent)
            parent = identity
        children = node.get("IORegistryEntryChildren", [])
        if not isinstance(children, list):
            raise RuntimeError("Unrecognized IOMedia registry children")
        pending.extend((child, parent) for child in children)
    return media


def previous_device_detached(device, selection):
    """Allow absent nodes or positively inactive retained IOMedia, without reads."""
    nodes = []
    for path in (device, "/dev/r" + Path(device).name):
        try:
            nodes.append(os.lstat(path))
        except FileNotFoundError:
            nodes.append(None)
        except OSError as error:
            raise RuntimeError("Cannot establish that the previous drive node is detached: " + path) from error
    if nodes == [None, None]:
        return "absent"
    block, raw = nodes
    if (block is None or raw is None or not stat.S_ISBLK(block.st_mode)
            or not stat.S_ISCHR(raw.st_mode) or block.st_rdev != raw.st_rdev):
        raise RuntimeError("Previous drive nodes still exist without a matching block/raw pair; mount unchanged")
    name = Path(device).name
    whole = re.fullmatch(r"(disk[0-9]+)s[0-9]+", name)
    if not whole:
        raise RuntimeError("Invalid previous partition device")
    whole = whole[1]
    active = plistlib.loads(run(["/usr/sbin/diskutil", "list", "-plist"]).stdout.encode())
    names = active.get("AllDisks") if isinstance(active, dict) else None
    if not isinstance(names, list) or not names or any(not isinstance(item, str) for item in names):
        raise RuntimeError("Active disk inventory did not establish previous device absence")
    if name in names or whole in names:
        raise RuntimeError("Previous device is still in the active disk inventory; mount unchanged")
    media = registry_media(plistlib.loads(run(["/usr/sbin/ioreg", "-r", "-c", "IOMedia", "-a"]).stdout.encode()))
    matches = {target: [(identity, entry) for identity, entry in media.items()
                        if entry["values"]["BSD Name"] == target] for target in (name, whole)}
    if any(len(entries) != 1 for entries in matches.values()):
        raise RuntimeError("Previous IOMedia parent/partition identity is missing or ambiguous")
    partition_id, partition_entry = matches[name][0]
    parent_id, parent_entry = matches[whole][0]
    partition, parent = partition_entry["values"], parent_entry["values"]
    if partition_entry["parents"] != {parent_id}:
        raise RuntimeError("Previous IOMedia partition ancestry is missing or ambiguous")
    for value in (partition, parent):
        state = value["IOServiceState"]
        # XNU IOKit/IOService.h: inactive=1, registered=2, matched=4.
        if (value["IOObjectClass"] != "IOMedia" or type(state) is not int or state < 0
                or not state & 1 or state & (2 | 4)):
            raise RuntimeError("Previous IOMedia is active or its inactive state is unverified")
        if (type(value["BSD Major"]) is not int or type(value["BSD Minor"]) is not int
                or value["BSD Major"] < 0 or value["BSD Minor"] < 0):
            raise RuntimeError("Previous IOMedia device numbers are invalid")
    if (partition["Whole"] is not False or parent["Whole"] is not True
            or type(partition["Size"]) is not int or type(parent["Size"]) is not int
            or partition["Size"] != selection["partition_bytes"] or parent["Size"] != selection["whole_bytes"]
            or canonical_uuid(partition["UUID"], "inactive partition UUID") != selection["partition_uuid"]):
        raise RuntimeError("Previous inactive IOMedia identity does not match the saved drive")
    numbers = (partition["BSD Major"], partition["BSD Minor"])
    if numbers != (os.major(block.st_rdev), os.minor(block.st_rdev)):
        raise RuntimeError("Previous device nodes no longer match inactive IOMedia device numbers")
    if any(identity != partition_id and (entry["values"]["BSD Major"], entry["values"]["BSD Minor"]) == numbers
           for identity, entry in media.items()):
        raise RuntimeError("Previous device numbers were reused by another IOMedia entry")
    return "inactive"


def stale_receipt(record, device=None):
    selection = record["selection"]
    owned = record.get("mount")
    if not isinstance(owned, dict):
        raise RuntimeError("No ownership receipt for this stale mount; leaving it unchanged")
    mode = access_mode(selection)
    if receipt_mode(owned) != mode:
        raise RuntimeError("Stale mount receipt does not match the selected access mode")
    previous = owned.get("device")
    if (not isinstance(previous, str) or not PARTITION.fullmatch(previous) or previous == device
            or owned.get("mounted") is not True
            or owned.get("filesystem") != "ext4"
            or not selection.get("filesystem_uuid")
            or any(owned.get(key) != selection.get(key)
                   for key in ("partition_uuid", "filesystem_uuid", "mountpoint"))
            or type(owned.get("host_mount_id")) is not int or owned["host_mount_id"] <= 0):
        raise RuntimeError("No matching ownership receipt for this stale mount; leaving it unchanged")
    expected_source = Path(previous).name + ".local:/mnt/" + Path(selection["mountpoint"]).name
    if owned.get("host_source") != expected_source:
        raise RuntimeError("Stale mount source does not match its ownership receipt")
    return owned


def owned_stale_mount(record, device, entries):
    """Inspect only host metadata; never contact a disconnected mount's guest."""
    mountpoint = record["selection"]["mountpoint"]
    mismatched = [entry for entry in entries if entry["mountpoint"] == mountpoint
                  and entry["device"] not in (device, Path(device).name)]
    if not mismatched:
        return None
    owned = stale_receipt(record, device)
    mode = access_mode(record["selection"])
    previous = owned["device"]
    entry = selected_mount(entries, previous, mountpoint)
    if not entry:
        raise RuntimeError("Stale anylinuxfs mount did not confirm the owned instance")
    check_mount_options(entry["options"], mode)
    if any(item["device"] in (device, Path(device).name) for item in entries):
        raise RuntimeError("Current drive is already mounted by another anylinuxfs instance")
    old_device_state = previous_device_detached(previous, record["selection"])
    if Path(mountpoint).is_symlink() or not os.path.ismount(mountpoint):
        raise RuntimeError("Stale host mount is not the owned real mount")
    if host_mount(mountpoint, mode) != owned["host_source"] or mount_id(mountpoint) != owned["host_mount_id"]:
        raise RuntimeError("Stale host mount no longer matches its exact ownership receipt")
    return {"selected": True, "connected": True, "mounted": True, "stale": True, "usable": False,
            "device": device, "current_device": device, "previous_device": previous,
            "old_device_state": old_device_state,
            "access_mode": mode, "read_only": mode == "ro",
            "mountpoint": mountpoint, "partition_uuid": owned["partition_uuid"],
            "filesystem_uuid": owned["filesystem_uuid"], "host_source": owned["host_source"],
            "host_mount_id": owned["host_mount_id"],
            "reconciliation_pending": bool(record.get("stale_unmount_attempt")),
            "reopen_hint": "Run o-linux drive open to reconcile the saved mount"}


def stale_mount_gone(owned, entries):
    previous, mountpoint = owned["device"], owned["mountpoint"]
    return (not os.path.ismount(mountpoint) and not any(
        entry["mountpoint"] == mountpoint or entry["device"] in (previous, Path(previous).name)
        for entry in entries))


def finish_stale_unmount(record):
    record["mount"] = None
    record.pop("stale_unmount_attempt", None)
    save_record(record)


def close_disconnected(app, record):
    """Release the exact owned inactive reader without opening a source device."""
    owned = stale_receipt(record)
    selection = record["selection"]
    mode = access_mode(selection)
    if record.get("mount_attempt"):
        raise RuntimeError("Prior mount completion is unknown; retaining its receipt")
    def inspect():
        status = any_status(app)
        previous_device_detached(owned["device"], selection)
        status_entry(status, owned["device"], owned["mountpoint"], mode)
        point = owned["mountpoint"]
        if (Path(point).is_symlink() or not os.path.ismount(point)
                or host_mount(point, mode) != owned["host_source"]
                or mount_id(point) != owned["host_mount_id"]):
            raise RuntimeError("Disconnected mount no longer matches its ownership receipt")
    pending = record.get("stale_unmount_attempt")
    if pending:
        if (not isinstance(pending, dict)
                or any(pending.get(key) != owned.get(key) for key in OWNED_MOUNT_KEYS)
                or access_mode(pending) != mode):
            raise RuntimeError("Disconnected unmount intent does not match its ownership receipt")
        if not stale_mount_gone(owned, parse_status(any_status(app))):
            raise RuntimeError("Prior unmount completion is unknown; no teardown repeated")
    else:
        require_sudo()
        inspect()
        record["stale_unmount_attempt"] = {**{key: owned[key] for key in OWNED_MOUNT_KEYS},
                                          "access_mode": mode, "operation_id": uuid.uuid4().hex,
                                          "action": "close-disconnected"}
        save_record(record)
        inspect()
        try:
            run(["/usr/bin/sudo", "-n", app, "unmount", owned["mountpoint"], "--wait-for-vm=20"], timeout=40)
            if not stale_mount_gone(owned, parse_status(any_status(app))):
                raise RuntimeError("Owned disconnected mount is still present")
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise RuntimeError("Disconnected unmount completion is unknown; receipt and intent retained: " + str(error)) from error
    finish_stale_unmount(record)
    return {"closed": True, "connected": False, "partition_uuid": selection["partition_uuid"],
            "mountpoint": owned["mountpoint"]}


def require_no_physical_export():
    """Retained raw writers must be reconciled before opening a filesystem."""
    path = state_dir() / "storage/state.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("Raw storage state is not a regular file; reconcile it before opening a drive")
    if not path.exists():
        return
    try:
        pending = json.loads(path.read_text())
        if not isinstance(pending, dict) or pending.get("source_mode") not in ("physical", "image"):
            raise ValueError("unknown source mode")
    except (OSError, ValueError) as error:
        raise RuntimeError("Raw storage state is unresolved; reconcile it before opening a drive") from error
    if pending["source_mode"] == "physical":
        raise RuntimeError("A physical-disk export is active or unresolved; close or reconcile it before opening a filesystem")


def reconcile_stale_mount(app, record, device, entries):
    """Detach only a positively identified stale instance, then allow normal open."""
    owned = stale_receipt(record, device)
    previous_device_detached(owned["device"], record["selection"])
    if record.get("mount_attempt"):
        raise RuntimeError("Prior mount completion is unknown; stale mount left unchanged")
    require_sudo()
    signature(device, record["selection"]["filesystem_uuid"])
    if locate(record["selection"]) != device:
        raise RuntimeError("Drive identity changed; stale mount left unchanged")
    pending = record.get("stale_unmount_attempt")
    if pending:
        if (not isinstance(pending, dict)
                or any(pending.get(key) != owned.get(key) for key in OWNED_MOUNT_KEYS)
                or access_mode(pending) != access_mode(record["selection"])):
            raise RuntimeError("Stale unmount intent does not match its ownership receipt")
        if not stale_mount_gone(owned, parse_status(any_status(app))):
            raise RuntimeError("Prior stale unmount completion is unknown; retaining its receipt without repeating teardown")
        finish_stale_unmount(record)
        return
    if not owned_stale_mount(record, device, parse_status(any_status(app))):
        raise RuntimeError("Stale mount changed before reconciliation; no teardown attempted")
    record["stale_unmount_attempt"] = {**{key: owned[key] for key in OWNED_MOUNT_KEYS},
                                        "access_mode": access_mode(record["selection"]),
                                        "replacement_device": device, "operation_id": uuid.uuid4().hex}
    save_record(record)
    try:
        if (locate(record["selection"]) != device
                or not owned_stale_mount(record, device, parse_status(any_status(app)))):
            raise RuntimeError("Drive or mount identity changed")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise RuntimeError("Stale mount changed after recording intent; no teardown dispatched; "
                           "pending intent retained: " + str(error)) from error
    try:
        run(["/usr/bin/sudo", "-n", app, "unmount", owned["mountpoint"], "--wait-for-vm=20"], timeout=40)
        if not stale_mount_gone(owned, parse_status(any_status(app))):
            raise RuntimeError("Owned stale mount is still present")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise RuntimeError("Stale unmount completion is unknown; retaining the ownership receipt and pending intent: "
                           + str(error)) from error
    finish_stale_unmount(record)


def operate(args):
    if args.action == "open":
        require_no_physical_export()
    if args.action == "select":
        if (state_dir() / "drives.json").exists():
            previous = load_record()
            if any(previous.get(key) for key in ("mount_attempt", "stale_unmount_attempt")):
                raise RuntimeError("A prior mount is pending; reconcile with open/status before changing selection")
            if previous.get("mount"):
                raise RuntimeError("Close the saved mount before changing selection")
        selection = selection_for(args.device, args.mountpoint or DEFAULT_MOUNT, args.access_mode or "rw")
        record = {"version": 1, "selection": selection, "mount": None}
        save_record(record)
        return {"selected": True, "mount_performed": False, "selection": selection}
    record = load_record()
    selection = record["selection"]
    mode = access_mode(selection)
    app = command("anylinuxfs", load_config())
    try:
        device = locate(selection)
    except RuntimeError as error:
        if str(error) != "Saved partition is not connected":
            raise
        if args.action == "close":
            return close_disconnected(app, record)
        if args.action == "status":
            return {"selected": True, "connected": False,
                    "mounted": os.path.ismount(selection["mountpoint"]),
                    "access_mode": mode, "selection": selection,
                    "mount_pending": bool(record.get("mount_attempt") or record.get("stale_unmount_attempt"))}
        if args.action != "mode":
            raise
        device = None
    status = any_status(app)
    mountpoint = selection["mountpoint"]
    entries = parse_status(status)
    if args.action == "mode":
        if (record.get("mount") or record.get("mount_attempt") or record.get("stale_unmount_attempt")
                or any(entry["mountpoint"] == mountpoint for entry in entries)
                or (device and selected_mount(entries, device, mountpoint)) or os.path.ismount(mountpoint)
                or (device and disk_info(device).get("MountPoint"))):
            raise RuntimeError("Close the saved mount and reconcile pending operations before changing its mode")
        selection["access_mode"] = args.device
        if args.mountpoint is not None:
            selection["mountpoint"] = mount_path(args.mountpoint)
        save_record(record)
        return {"selected": True, "mount_performed": False, "access_mode": args.device,
                "read_only": args.device == "ro", "selection": selection}
    stale = owned_stale_mount(record, device, entries)
    if stale and args.action == "status":
        return stale
    if stale or record.get("stale_unmount_attempt"):
        if args.action == "open":
            reconcile_stale_mount(app, record, device, entries)
            status = any_status(app)
            entries = parse_status(status)
        elif args.action != "status":
            raise RuntimeError("Saved mount is stale; run o-linux drive open to reconcile it")
    active = selected_mount(entries, device, mountpoint)
    if args.action == "status" and not active and not os.path.ismount(mountpoint):
        return {"selected": True, "connected": True, "mounted": False, "device": device,
                "access_mode": mode, "read_only": mode == "ro",
                "selection": selection, "mount_pending": bool(record.get("mount_attempt")),
                "reconciliation_pending": bool(record.get("stale_unmount_attempt"))}
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
                                   "access_mode": mode,
                                   "operation_id": uuid.uuid4().hex}
        save_record(record)
        try:
            run(["/usr/bin/sudo", "-n", app, Path(device).name, mountpoint, "-t", "ext4", "-o",
                 "ro,noload" if mode == "ro" else "rw", "-n", mode,
                 "--ignore-permissions", "--net-helper", "gvproxy", "-b", "127.0.0.1",
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
        if (not owned or any(owned.get(key) != verified.get(key) for key in keys)
                or receipt_mode(owned) != mode):
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
    parser.add_argument("action", nargs="?", choices=("select", "open", "status", "close", "mode"), default="open")
    parser.add_argument("device", nargs="?", help="Exact partition for select, or ro/rw for mode")
    parser.add_argument("--mountpoint", help="Saved directory under /Volumes for select or an unmounted mode change")
    parser.add_argument("--filesystem", choices=("ext4",), default="ext4")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--read-only", dest="access_mode", action="store_const", const="ro")
    modes.add_argument("--read-write", dest="access_mode", action="store_const", const="rw")
    args = parser.parse_args(argv)
    if args.action == "mode":
        if args.device not in ("ro", "rw"):
            parser.error("mode requires ro or rw")
    elif (args.action == "select") != bool(args.device):
        parser.error("select requires a partition; open/status/close use the saved selection")
    if args.access_mode is not None and args.action != "select":
        parser.error("--read-only/--read-write apply to select; use mode ro/rw on an unmounted saved selection")
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
