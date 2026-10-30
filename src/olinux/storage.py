#!/usr/bin/env python3
"""Expose a selected raw image or explicitly identified external disk read-only.

The NBD server binds host loopback only. A dedicated SSH key permits only a
loopback reverse tunnel; Multipass performs guest administration. Physical disks
require --device and --expected-size; mounted or compound storage is refused.
Ctrl-C disconnects the mapping and tunnel. No filesystem is mounted here.
"""

import argparse
import array
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from .config import command, load_config, state_dir

BASE = None
MP = None
QEMU_NBD = None
GUEST_USER = None
HOST_PORT = 10829
GUEST_PORT = 10819
EXPORT = "o-linux-readonly"
KEY = KNOWN = STATE = None
IDENTITY_FIELDS = ("DeviceIdentifier", "DeviceNode", "WholeDisk", "Internal",
                   "VirtualOrPhysical", "TotalSize", "DeviceBlockSize",
                   "BusProtocol", "MediaName", "IORegistryEntryName",
                   "DeviceTreePath", "MediaUUID", "DiskUUID", "SerialNumber")


def configure(settings):
    """Resolve local runtime paths only when starting a bridge."""
    global BASE, MP, QEMU_NBD, GUEST_USER, HOST_PORT, GUEST_PORT, KEY, KNOWN, STATE
    BASE = state_dir() / "storage"
    MP = command("multipass", settings)
    QEMU_NBD = command("qemu-nbd", settings)
    GUEST_USER = settings["guest_user"]
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", GUEST_USER):
        raise RuntimeError("Invalid configured guest user")
    ports = [settings.get("nbd_host_port", 10829), settings.get("nbd_guest_port", 10819)]
    if any(isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535 for port in ports):
        raise RuntimeError("NBD ports must be integers between 1024 and 65535")
    HOST_PORT, GUEST_PORT = ports
    KEY = BASE / "id_ed25519"
    KNOWN = BASE / "known_hosts"
    STATE = BASE / "state.json"


def run(argv, *, data=None, timeout=25, check=True):
    p = subprocess.run(argv, input=data, text=True, capture_output=True,
                       timeout=timeout, start_new_session=True)
    if check and p.returncode:
        raise RuntimeError(f"{argv[0]} failed ({p.returncode}): "
                           f"{p.stderr.strip() or p.stdout.strip()}")
    return p


def disk_plist(*args):
    return plistlib.loads(run(["/usr/sbin/diskutil", *args]).stdout.encode())


def inspect_device(device, expected_size):
    """Read OS metadata only; never open media or unmount a volume."""
    if not re.fullmatch(r"/dev/disk[0-9]+", device):
        raise RuntimeError("Use an exact whole-disk /dev/diskN path, never a partition or rdisk alias")
    if expected_size <= 0:
        raise RuntimeError("--expected-size must be an exact positive byte count")
    info = disk_plist("info", "-plist", device)
    if (info.get("DeviceNode") != device or info.get("WholeDisk") is not True
            or info.get("Internal") is not False
            or info.get("VirtualOrPhysical") != "Physical"):
        raise RuntimeError("Device must be a whole, external, physical disk")
    if info.get("TotalSize") != expected_size:
        raise RuntimeError(f"Size mismatch: expected {expected_size}; current {info.get('TotalSize')}")
    if not info.get("DeviceTreePath") or not info.get("MediaName"):
        raise RuntimeError("macOS did not supply enough current device identity metadata")
    listing = disk_plist("list", "-plist", device)
    names = set()
    def walk(value):
        if isinstance(value, dict):
            content = str(value.get("Content", value.get("Type", "")))
            if any(s in content for s in ("Apple_APFS", "Apple_CoreStorage", "Apple_RAID")):
                raise RuntimeError("Compound APFS/CoreStorage/RAID source is not supported by this physical bridge; use a raw image")
            if value.get("DeviceIdentifier"):
                names.add(value["DeviceIdentifier"])
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(listing.get("AllDisksAndPartitions", []))
    if info["DeviceIdentifier"] not in names:
        raise RuntimeError("Cannot verify the disk's complete partition listing")
    for name in names:
        if not re.fullmatch(re.escape(info["DeviceIdentifier"]) + r"(?:s[0-9]+)*", name):
            raise RuntimeError("Unresolved synthesized/compound child storage; refusing export")
        child = info if name == info["DeviceIdentifier"] else disk_plist("info", "-plist", "/dev/" + name)
        if child.get("Mounted") or child.get("MountPoint"):
            raise RuntimeError(f"{name} is mounted; no automatic unmount is performed")
        if any(s in str(child.get("Content", "")) for s in ("Apple_APFS", "Apple_CoreStorage", "Apple_RAID")):
            raise RuntimeError("Compound source storage is not supported by this physical bridge")
    st = os.lstat(device)
    if not stat.S_ISBLK(st.st_mode):
        raise RuntimeError("Expected a block-device node, not a symlink, regular file, or character device")
    identity = {key: info.get(key) for key in IDENTITY_FIELDS}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {"identity": identity, "fingerprint": digest,
            "device_stat": [st.st_rdev, st.st_ino, st.st_ctime_ns],
            "children": sorted(names)}


DEVICE_OPENER = r"""
import array,json,os,plistlib,socket,stat,subprocess,sys
device,sockpath,serialized=sys.argv[1:]
expected=json.loads(serialized)
def check():
    raw=subprocess.check_output(['/usr/sbin/diskutil','info','-plist',device],timeout=15)
    info=plistlib.loads(raw)
    actual={key:info.get(key) for key in expected['identity']}
    if actual!=expected['identity']: raise RuntimeError('Device identity changed before privileged open')
    st=os.lstat(device)
    if not stat.S_ISBLK(st.st_mode): raise RuntimeError('Source is no longer a block device')
    if [st.st_rdev,st.st_ino,st.st_ctime_ns]!=expected['device_stat']:
        raise RuntimeError('Device node was replaced')
check()
fd=os.open(device,os.O_RDONLY|os.O_NOFOLLOW)
try:
    st=os.fstat(fd)
    if not stat.S_ISBLK(st.st_mode) or [st.st_rdev,st.st_ino,st.st_ctime_ns]!=expected['device_stat']:
        raise RuntimeError('Opened device identity mismatch')
    check()
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.connect(sockpath)
        s.sendmsg([b'R'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])
finally: os.close(fd)
"""


def open_physical(device, expected_size, before):
    """Privilege only the short open/FD-transfer helper, not QEMU or SSH."""
    received = None
    with tempfile.TemporaryDirectory(prefix="nbd_open_", dir=BASE) as temp:
        os.chmod(temp, 0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            path = str(Path(temp) / "fd.sock")
            listener.bind(path)
            listener.listen(1)
            listener.settimeout(0.2)
            helper = subprocess.Popen(
                ["/usr/bin/sudo", "-n", sys.executable, "-I", "-c", DEVICE_OPENER,
                 device, path, json.dumps(before)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                deadline = time.monotonic() + 35
                while True:
                    try:
                        connection, _ = listener.accept()
                        break
                    except socket.timeout:
                        if helper.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("Privileged read-only open failed: " + helper.communicate(timeout=2)[1])
                with connection:
                    connection.settimeout(3)
                    message, ancillary, _, _ = connection.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize))
                    if message != b'R': raise RuntimeError("Unexpected FD-transfer response")
                    for level, kind, data in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            values = array.array('i')
                            values.frombytes(data[:len(data) - (len(data) % values.itemsize)])
                            if len(values) == 1: received = values[0]
                    if received is None: raise RuntimeError("Privileged helper did not return a descriptor")
                _, error = helper.communicate(timeout=5)
                if helper.returncode: raise RuntimeError(error)
                st = os.fstat(received)
                if (not stat.S_ISBLK(st.st_mode)
                        or [st.st_rdev, st.st_ino, st.st_ctime_ns] != before["device_stat"]
                        or fcntl.fcntl(received, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY):
                    raise RuntimeError("Transferred descriptor is not the selected read-only disk")
                after = inspect_device(device, expected_size)
                if after != before: raise RuntimeError("Disk identity or partitions changed during open")
                return received
            except BaseException:
                if received is not None: os.close(received)
                raise
            finally:
                stop(helper)


def running_instance(instance):
    items = json.loads(run([MP, "list", "--format", "json"]).stdout)["list"]
    item = next((v for v in items if v["name"] == instance), None)
    if not item or item.get("state") != "Running":
        raise RuntimeError(f"Multipass {instance!r} must already be Running; "
                           "this bridge does not start or restart VMs")
    ips = item.get("ipv4", [])
    for value in ips:
        ip = ipaddress.ip_address(value)
        if ip.version == 4 and not ip.is_loopback:
            return str(ip)
    raise RuntimeError("The running guest has no usable IPv4 address")


def guest(instance, args, *, data=None, timeout=25, check=True):
    # multipass exec can auto-start a stopped VM, so check before every call.
    running_instance(instance)
    args = list(args)
    # This guest's sudo command adds a shell layer. Send privileged Python on
    # stdin so neither that shell nor Multipass has to quote program source.
    if args[:4] == ["sudo", "-n", "python3", "-c"]:
        if data is not None:
            raise RuntimeError("Privileged helper already uses stdin")
        data = args[4]
        args = ["sudo", "-n", "python3", "-", *args[5:]]
    elif "-c" in args:
        index = args.index("-c") + 1
        encoded = base64.b64encode(args[index].encode()).decode()
        args[index] = f"exec(__import__('base64').b64decode('{encoded}'))"
    return run([MP, "exec", instance, "--", *args], data=data,
               timeout=timeout, check=check)


AUTHORIZE_KEY = r"""
import os,pathlib,shlex,stat,sys,tempfile
p=pathlib.Path.home()/'.ssh'
if p.is_symlink(): raise RuntimeError('Refusing a symlinked .ssh directory')
p.mkdir(mode=0o700,exist_ok=True)
if not p.is_dir(): raise RuntimeError('Expected a .ssh directory')
a=p/'authorized_keys'
line=sys.stdin.buffer.read().strip()
parts=shlex.split(line.decode())
keytype,key=parts[1:3]
previous=None
if a.exists() or a.is_symlink():
    previous=a.lstat()
    if not stat.S_ISREG(previous.st_mode): raise RuntimeError('Refusing non-regular authorized_keys')
    fd=os.open(a,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as source:
        current=os.fstat(source.fileno())
        if (current.st_dev,current.st_ino)!=(previous.st_dev,previous.st_ino):
            raise RuntimeError('authorized_keys changed while opening')
        old=source.read()
else: old=b''
def owned(row):
    try: words=shlex.split(row.decode('utf-8',errors='surrogateescape'),comments=True)
    except ValueError: return False
    offset=0 if words and words[0]==keytype else 1
    return len(words)>offset+1 and words[offset:offset+2]==[keytype,key]
kept=b''.join(row for row in old.splitlines(keepends=True) if not owned(row))
if kept and not kept.endswith(b'\n'): kept+=b'\n'
payload=kept+line+b'\n'
fd,name=tempfile.mkstemp(prefix='.authorized_keys.',dir=p)
try:
    with os.fdopen(fd,'wb') as target:
        os.fchmod(target.fileno(),stat.S_IMODE(previous.st_mode) if previous else 0o600)
        if previous: os.fchown(target.fileno(),previous.st_uid,previous.st_gid)
        target.write(payload); target.flush(); os.fsync(target.fileno())
    if previous:
        current=a.lstat()
        if (current.st_dev,current.st_ino,current.st_mtime_ns,current.st_size)!=(previous.st_dev,previous.st_ino,previous.st_mtime_ns,previous.st_size):
            raise RuntimeError('authorized_keys changed before replacement')
    elif a.exists() or a.is_symlink(): raise RuntimeError('authorized_keys appeared before replacement')
    os.replace(name,a)
    directory=os.open(p,os.O_RDONLY)
    try: os.fsync(directory)
    finally: os.close(directory)
finally:
    if os.path.exists(name): os.unlink(name)
"""


def prepare_ssh(instance):
    if not KEY.exists():
        run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "",
             "-C", "peripheral-access-nbd-tunnel", "-f", str(KEY)])
    if KEY.is_symlink() or not stat.S_ISREG(KEY.stat().st_mode):
        raise RuntimeError("Refusing a non-regular SSH key")
    KEY.chmod(0o600)
    pub = run(["/usr/bin/ssh-keygen", "-y", "-f", str(KEY)]).stdout.strip()
    entry = (f'restrict,port-forwarding,permitlisten="127.0.0.1:{GUEST_PORT}",'
             'permitopen="127.0.0.1:1",command="/bin/false" '
             + pub + " peripheral-access-nbd-tunnel")
    guest(instance, ["python3", "-c", AUTHORIZE_KEY], data=entry)
    # Trust a public host key obtained through the authenticated Multipass API.
    host_key = guest(instance, ["cat", "/etc/ssh/ssh_host_ed25519_key.pub"]).stdout.split()
    if len(host_key) < 2 or host_key[0] != "ssh-ed25519":
        raise RuntimeError("Guest did not return an Ed25519 host public key")
    alias = "peripheral-nbd-" + instance
    KNOWN.write_text(alias + " " + " ".join(host_key[:2]) + "\n")
    KNOWN.chmod(0o600)
    return alias


GUEST_RECEIPTS = r"""
import fcntl,json,os,pathlib,re,stat,subprocess,sys,tempfile,time
instance,operation=sys.argv[1:3]
if not re.fullmatch('[0-9a-f]{32}',operation): raise RuntimeError('Invalid storage operation ID')
receipts=pathlib.Path('/run/o-linux-nbd')
blocks=pathlib.Path('/sys/class/block')
processes=pathlib.Path('/proc')
if receipts.is_symlink(): raise RuntimeError('Refusing symlinked receipt directory')
receipts.mkdir(mode=0o700,exist_ok=True)
info=receipts.stat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077:
    raise RuntimeError('Receipt directory is not private to the helper')
lock=os.fdopen(os.open(receipts/'lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600),'r+')
fcntl.flock(lock,fcntl.LOCK_EX)
path=receipts/(operation+'.json')
def save(receipt):
    fd,name=tempfile.mkstemp(prefix=operation+'.',dir=receipts)
    try:
        with os.fdopen(fd,'w') as output:
            json.dump(receipt,output); output.write('\n'); output.flush(); os.fsync(output.fileno())
        os.replace(name,path)
        directory=os.open(receipts,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)
def load():
    if path.is_symlink(): raise RuntimeError('Refusing symlinked operation receipt')
    if not path.exists(): return None
    receipt=json.loads(path.read_text())
    if receipt.get('operation_id')!=operation or receipt.get('instance')!=instance:
        raise RuntimeError('Storage receipt belongs to another operation or instance')
    if receipt.get('device') and not re.fullmatch('/dev/nbd[0-9]+',receipt['device']):
        raise RuntimeError('Invalid NBD device in receipt')
    return receipt
def current_pid(device):
    path=blocks/pathlib.Path(device).name/'pid'
    return path.read_text().strip() if path.exists() else None
def start_time(pid):
    if not re.fullmatch('[1-9][0-9]*',pid): raise RuntimeError('Invalid client PID')
    return (processes/pid/'stat').read_text().rsplit(')',1)[1].split()[19]
def inactive(device):
    path=blocks/pathlib.Path(device).name
    return current_pid(device) is None and (not path.exists() or (path/'size').read_text().strip()=='0')
def reply(receipt): print(json.dumps(receipt),flush=True)
"""

CONNECT = GUEST_RECEIPTS + r"""
port,export=sys.argv[3:]
receipt=load()
if receipt is not None: raise RuntimeError('Storage operation already exists; reconcile its receipt before retrying')
receipt={'operation_id':operation,'instance':instance,'status':'pending','export':export}
save(receipt)
subprocess.run(['modprobe','nbd','nbds_max=16','max_part=16'],check=True,capture_output=True,text=True)
subprocess.run(['udevadm','settle','--timeout=10'],check=True,timeout=12,capture_output=True,text=True)
device=None
for p in sorted(blocks.glob('nbd[0-9]*'),key=lambda p:int(p.name[3:]) if p.name[3:].isdigit() else 99999):
    if p.name[3:].isdigit() and inactive('/dev/'+p.name):
        device='/dev/'+p.name
        break
if device is None: raise RuntimeError('No unused NBD device is available')
receipt['device']=device
save(receipt)
# Netlink leaves a historical creator PID; ioctl mode keeps a live owner daemon.
result=subprocess.run(['nbd-client','-L','-N',export,'-R','-t','10','127.0.0.1',port,device],text=True,capture_output=True,timeout=20)
if result.returncode: raise RuntimeError(result.stdout+result.stderr)
deadline=time.monotonic()+5
while time.monotonic()<deadline:
    pid=current_pid(device)
    if pid:
        try: started=start_time(pid)
        except FileNotFoundError: pass
        else:
            if current_pid(device)==pid: break
    time.sleep(0.05)
else: raise RuntimeError('NBD attachment has no verifiable live owner daemon')
receipt.update(status='attached',client_pid=pid,client_start=started)
save(receipt)
readonly=subprocess.check_output(['blockdev','--getro',device],text=True,timeout=10).strip()
if readonly!='1': raise RuntimeError('Guest did not report a read-only block device')
receipt['read_only']=True
save(receipt)
reply(receipt)
"""

DISCONNECT = GUEST_RECEIPTS + r"""
expected_device,expected_pid=(value if value!='-' else '' for value in sys.argv[3:])
receipt=load()
if receipt is None:
    # A tombstone also prevents a delayed attach from starting after cleanup.
    receipt={'operation_id':operation,'instance':instance,'status':'cancelled'}
elif receipt['status'] not in ('cancelled','detached'):
    device=receipt.get('device')
    if expected_device and (device!=expected_device or receipt.get('client_pid')!=expected_pid):
        raise RuntimeError('Host and guest mapping receipts disagree')
    if device and not inactive(device):
        pid=receipt.get('client_pid')
        if not pid or not receipt.get('client_start'):
            raise RuntimeError('Attachment ownership is unknown; retaining pending receipt')
        if current_pid(device)!=pid or start_time(pid)!=receipt['client_start']:
            raise RuntimeError('NBD ownership changed; refusing to disconnect another mapping')
        subprocess.run(['nbd-client','-L','-d',device],check=True,timeout=15,capture_output=True,text=True)
        deadline=time.monotonic()+5
        while not inactive(device) and time.monotonic()<deadline: time.sleep(0.1)
        if not inactive(device): raise RuntimeError('NBD disconnect could not be verified')
    receipt['status']='detached' if device else 'cancelled'
receipt['clean']=True
save(receipt)
reply(receipt)
"""


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def open_image(path):
    """Pin a nonempty regular file with a read-only descriptor."""
    source = Path(path).expanduser().resolve(strict=True)
    if source == Path('/dev') or Path('/dev') in source.parents:
        raise RuntimeError("Image mode rejects devices; use explicit --device and --expected-size")
    if not source.is_file():
        raise RuntimeError("Image must be a regular file")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_size == 0:
            raise RuntimeError("Image must be a nonempty regular file")
        if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
            raise RuntimeError("Image descriptor is not read-only")
        return source, fd
    except BaseException:
        os.close(fd)
        raise


def server_arguments(fd, export=EXPORT):
    return [QEMU_NBD, "--read-only", "--format=raw", "--bind=127.0.0.1",
            f"--port={HOST_PORT}", f"--export-name={export}", "--persistent",
            "--shared=1", f"/dev/fd/{fd}"]


def save_state(state):
    fd, name = tempfile.mkstemp(prefix=".state.", dir=BASE)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(state, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, STATE)
        directory = os.open(BASE, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def receipt_response(output, state, *, clean=False):
    receipt = json.loads(output)
    if (not isinstance(receipt, dict)
            or receipt.get("operation_id") != state["operation_id"]
            or receipt.get("instance") != state["instance"]):
        raise RuntimeError("Guest receipt does not identify this storage operation")
    if clean:
        if receipt.get("clean") is not True or receipt.get("status") not in ("detached", "cancelled"):
            raise RuntimeError("Guest did not confirm storage cleanup")
    elif (receipt.get("status") != "attached" or receipt.get("read_only") is not True
          or not re.fullmatch(r"/dev/nbd[0-9]+", str(receipt.get("device", "")))
          or not re.fullmatch(r"[1-9][0-9]*", str(receipt.get("client_pid", "")))
          or not re.fullmatch(r"[0-9]+", str(receipt.get("client_start", "")))
          or receipt.get("export") != state["export"]):
        raise RuntimeError("Guest did not confirm an owned read-only attachment")
    return receipt


def attach(instance, state):
    """Write intent before execution so lost client output cannot lose ownership."""
    save_state(state)
    connected = guest(instance, ["sudo", "-n", "python3", "-c", CONNECT,
                                instance, state["operation_id"], str(GUEST_PORT),
                                state["export"]], timeout=45)
    state.update(receipt_response(connected.stdout, state))
    save_state(state)
    return state


def disconnect_operation(instance, state):
    result = guest(instance, ["sudo", "-n", "python3", "-c", DISCONNECT,
                             instance, state["operation_id"], state.get("device") or "-",
                             state.get("client_pid") or "-"], timeout=45)
    return receipt_response(result.stdout, state, clean=True)


def cleanup(instance, state, tunnel, server, fd):
    """Release only the mapping owned by this process; retain failed cleanup evidence."""
    clean = True
    if state:
        try:
            disconnect_operation(instance, state)
        except Exception as error:
            clean = False
            print(f"Guest cleanup could not be verified: {error}", file=sys.stderr)
    try:
        stop(tunnel)
    finally:
        try:
            stop(server)
        finally:
            if fd is not None:
                os.close(fd)
    if clean and STATE.exists():
        STATE.unlink()
    if not clean:
        print(f"Retained pending operation: {STATE}\n"
              "Retry receipt-based cleanup with: linux image --cleanup-pending", file=sys.stderr)
    return clean


def cleanup_pending(instance=None):
    """Reconcile a retained operation without reopening its source."""
    if not STATE.exists():
        print("No pending storage operation.")
        return
    state = json.loads(STATE.read_text())
    if not re.fullmatch(r"[0-9a-f]{32}", str(state.get("operation_id", ""))):
        raise RuntimeError(f"State has no verifiable operation ID; inspect {STATE}")
    if instance and state["instance"] != instance:
        raise RuntimeError("Pending operation belongs to a different instance")
    result = disconnect_operation(state["instance"], state)
    # Recorded PIDs alone cannot safely authorize terminating old host processes.
    alive = []
    for key in ("server_pid", "tunnel_pid"):
        pid = state.get(key)
        if not isinstance(pid, int) or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            alive.append(pid)
        else:
            alive.append(pid)
    if alive:
        state.update(result)
        save_state(state)
        raise RuntimeError(f"Guest mapping is detached. Host PIDs {alive} still exist; "
                           f"verify their identity before stopping them, then retry. Retained {STATE}")
    STATE.unlink()
    print("Pending storage operation is detached; source was not reopened.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="linux image", description=__doc__)
    parser.add_argument("image", nargs="?", help="Existing regular RAW disk image; never /dev/disk*")
    parser.add_argument("--device", help="Explicit whole external physical disk, /dev/diskN")
    parser.add_argument("--expected-size", type=int, help="Required exact byte size for --device, from a current inventory")
    parser.add_argument("--instance", help="Override the configured Linux instance")
    parser.add_argument("--cleanup-pending", action="store_true",
                        help="Reconcile a retained operation using its guest receipt; never reopen its source")
    args = parser.parse_args(argv)
    if args.cleanup_pending and (args.image or args.device or args.expected_size is not None):
        parser.error("--cleanup-pending cannot be combined with a source")
    if not args.cleanup_pending and bool(args.image) == bool(args.device):
        parser.error("Choose exactly one regular image or --device")
    if args.device and args.expected_size is None:
        parser.error("--device requires --expected-size from a current inventory")
    if args.image and args.expected_size is not None:
        parser.error("--expected-size is only valid with --device")
    settings = load_config()
    configure(settings)
    physical = None
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (BASE / "nbd_bridge.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("Another image bridge is already running")
    if args.cleanup_pending:
        try:
            cleanup_pending(args.instance)
        finally:
            lock.close()
        return
    args.instance = args.instance or settings["instance"]
    if STATE.exists():
        parser.error(f"An earlier mapping needs cleanup; run linux image --cleanup-pending or inspect {STATE}")
    if args.device:
        physical = inspect_device(args.device, args.expected_size)
        source = Path(args.device)
        print("Selected external disk: " + json.dumps(physical, sort_keys=True), flush=True)
    else:
        source = Path(args.image).expanduser()
    if physical:
        fd = open_physical(args.device, args.expected_size, physical)
    else:
        source, fd = open_image(source)
    server = tunnel = None
    state = None
    operation = uuid.uuid4().hex
    export = EXPORT + "-" + operation
    stopping = False
    def request_stop(*_):
        nonlocal stopping
        stopping = True
    # Finish any in-flight attachment before cleanup so ownership is known.
    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)
    try:
        ip = running_instance(args.instance)
        alias = prepare_ssh(args.instance)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", HOST_PORT))
        server_log = (BASE / "nbd_server.log").open("w")
        tunnel_log = (BASE / "nbd_tunnel.log").open("w")
        # Inherited read-only FD pins the selected file against path replacement.
        server = subprocess.Popen(
            server_arguments(fd, export), pass_fds=(fd,),
            stdout=server_log, stderr=server_log, start_new_session=True)
        for _ in range(40):
            if server.poll() is not None:
                raise RuntimeError("NBD server exited; see nbd_server.log")
            try:
                with socket.create_connection(("127.0.0.1", HOST_PORT), 0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("NBD server did not begin listening")
        tunnel = subprocess.Popen(
            ["/usr/bin/ssh", "-F", "/dev/null", "-N", "-T", "-i", str(KEY),
             "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
             "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={KNOWN}",
             "-o", "GlobalKnownHostsFile=/dev/null", "-o", f"HostKeyAlias={alias}",
             "-o", "ConnectTimeout=10", "-o", "ExitOnForwardFailure=yes",
             "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
             "-R", f"127.0.0.1:{GUEST_PORT}:127.0.0.1:{HOST_PORT}", f"{GUEST_USER}@{ip}"],
            stdout=tunnel_log, stderr=tunnel_log, start_new_session=True)
        time.sleep(1)
        if tunnel.poll() is not None:
            raise RuntimeError("SSH tunnel exited; see nbd_tunnel.log")
        if stopping:
            raise KeyboardInterrupt()
        state = dict(operation_id=operation, status="pending", export=export,
                     source=str(source), source_mode="physical" if physical else "image",
                     source_identity=physical, instance=args.instance,
                     host_pid=os.getpid(), server_pid=server.pid, tunnel_pid=tunnel.pid,
                     host_port=HOST_PORT, guest_port=GUEST_PORT, format="raw",
                     guest_receipt=f"/run/o-linux-nbd/{operation}.json")
        mapping = attach(args.instance, state)
        print(f"Read-only {'physical disk' if physical else 'RAW image'}: {source}", flush=True)
        print(f"Linux {args.instance}: {mapping['device']} (read_only=1)", flush=True)
        print("No filesystem mounted. Ctrl-C disconnects this mapping and tunnel.", flush=True)
        if physical:
            print("This export blocks guest writes; independent macOS writes are not globally blocked.", flush=True)
        next_identity_check = time.monotonic() + 5
        while not stopping and server.poll() is None and tunnel.poll() is None:
            time.sleep(0.5)
            if physical and time.monotonic() >= next_identity_check:
                if inspect_device(args.device, args.expected_size) != physical:
                    raise RuntimeError("Physical disk identity or partitions changed; stopping export")
                next_identity_check = time.monotonic() + 5
        if stopping:
            raise KeyboardInterrupt()
        raise RuntimeError("NBD server or SSH tunnel exited unexpectedly")
    except KeyboardInterrupt:
        print("\nDisconnecting read-only storage bridge...", flush=True)
    finally:
        try:
            cleaned = cleanup(args.instance, state, tunnel, server, fd)
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            lock.close()
        if not cleaned:
            raise RuntimeError(f"Storage disconnect is incomplete; inspect {STATE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Storage bridge failed: {error}", file=sys.stderr)
        sys.exit(1)
