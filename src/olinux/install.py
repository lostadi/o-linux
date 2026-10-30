"""Install the persistent macOS guest and its two authenticated command paths."""
import argparse
import base64
import fcntl
import getpass
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

from .config import state_dir

PACKAGES = ["python3", "git", "curl", "rsync", "build-essential", "usbutils",
            "pciutils", "nbd-client", "gddrescue", "smartmontools", "file", "socat", "jq",
            "qemu-system-arm", "qemu-system-x86", "qemu-efi-aarch64", "ovmf"]


def run(argv, *, data=None, timeout=60, check=True):
    result = subprocess.run(argv, input=data, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed ({result.returncode}): "
                           + (result.stderr.strip() or result.stdout.strip()))
    return result


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("Refusing to replace a symlink: " + str(path))
    raw = data.encode() if isinstance(data, str) else data
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def public_key(text):
    fields = text.strip().split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise RuntimeError("An Ed25519 public key was required")
    try:
        base64.b64decode(fields[1], validate=True)
    except ValueError as error:
        raise RuntimeError("Invalid public key encoding") from error
    return " ".join(fields[:2])


def pin_host_key(path, alias, key):
    line = alias + " " + public_key(key) + "\n"
    path = Path(path)
    if path.exists() and path.read_text() != line:
        raise RuntimeError("SSH host identity changed; refusing to replace " + str(path))
    atomic_write(path, line)


def authorized_key_bytes(previous, key, entry):
    """Replace only this key's line; all unrelated bytes remain unchanged."""
    token = public_key(key).split()[1].encode()
    result = []
    found = False
    for line in previous.splitlines(keepends=True):
        if token in line.split():
            if not found:
                result.append(entry.encode() + b"\n")
                found = True
        else:
            result.append(line)
    if not found:
        if result and not result[-1].endswith((b"\n", b"\r")):
            result.append(b"\n")
        result.append(entry.encode() + b"\n")
    return b"".join(result)


def add_host_key(path, key, entry, state):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("Refusing a symlinked authorized_keys file")
    with (state / "authorized_keys.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = path.read_bytes() if path.exists() else b""
        updated = authorized_key_bytes(previous, key, entry)
        if previous == updated:
            return
        if previous:
            backup = state / "backups" / ("authorized_keys-" + str(time.time_ns()))
            atomic_write(backup, previous)
        if (path.read_bytes() if path.exists() else b"") != previous:
            raise RuntimeError("authorized_keys changed during setup; rerun setup")
        atomic_write(path, updated)


def ensure_key(path):
    if path.exists():
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise RuntimeError("Refusing a non-regular private key: " + str(path))
        path.chmod(0o600)
    else:
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "o-linux", "-f", str(path)])
    key = public_key(run(["ssh-keygen", "-y", "-f", str(path)]).stdout)
    atomic_write(Path(str(path) + ".pub"), key + "\n", 0o644)
    return key


def cloud_config(config):
    home = config["host_home"]
    guest_home = config["guest_home"]
    owner = config["guest_user"] + ":" + config["guest_user"]
    document = {"package_update": True, "package_upgrade": False, "packages": PACKAGES,
                "runcmd": [["mkdir", "-p", home, "/Volumes", "/host"],
                           ["ln", "-sfn", home, "/host/home"],
                           ["ln", "-sfn", "/Volumes", "/host/volumes"],
                           ["mkdir", "-p", guest_home + "/.local/bin"],
                           ["chown", "-R", owner, guest_home + "/.local"]]}
    # JSON is valid YAML, avoiding a host dependency just to render cloud-init.
    return "#cloud-config\n" + json.dumps(document, indent=2) + "\n"


def launcher_text(state):
    return ("#!/usr/bin/env python3\n# o-linux managed launcher\n"
            "import os, sys\n"
            f"os.environ.setdefault('O_LINUX_STATE', {str(state)!r})\n"
            f"sys.path.insert(0, {str(state / 'app/src')!r})\n"
            "from olinux.cli import entry\nraise SystemExit(entry())\n")


def check_launcher(path, text, replace):
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir():
        raise RuntimeError("Launcher path is a directory: " + str(path))
    current = path.read_text() if path.is_file() else ""
    if current == text:
        return
    if not replace:
        raise RuntimeError(str(path) + " already exists; use --replace-launcher to back it up and replace it")


def install_launcher(path, text, state, replace=False):
    check_launcher(path, text, replace)
    if path.exists() or path.is_symlink():
        if path.is_file() and path.read_text() == text:
            return
        backup = state / "backups" / (path.name + "-" + str(time.time_ns()))
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup, follow_symlinks=False)
    # An explicitly replaced launcher may have been a symlink; replace its name.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".o-linux-" + str(time.time_ns()))
    atomic_write(temporary, text, 0o755)
    os.replace(temporary, path)


def install_app(repository, state):
    temporary = state / ("app-stage-" + str(time.time_ns()))
    (temporary / "src/olinux").mkdir(parents=True)
    try:
        sources = list((repository / "src/olinux").glob("*.py"))
        if not sources:
            raise RuntimeError("Package source is missing")
        for source in sources:
            if source.is_symlink():
                raise RuntimeError("Refusing symlinked package source: " + str(source))
            shutil.copy2(source, temporary / "src/olinux" / source.name)
        templates = repository / "templates"
        if templates.exists():
            for source in templates.glob("*"):
                if source.is_file() and source.suffix in (".yaml", ".json") and not source.is_symlink():
                    (temporary / "templates").mkdir(exist_ok=True)
                    shutil.copy2(source, temporary / "templates" / source.name)
        # This is an explicit integration allowlist, never a recursive copy of
        # local O programs, runtime state, or private evidence.
        relative = Path("integrations/ostadix/read-drive.O")
        source = repository / relative
        if source.exists() or source.is_symlink():
            if source.is_symlink() or not source.is_file():
                raise RuntimeError("Refusing non-regular integration source: " + str(source))
            target = temporary / relative
            target.parent.mkdir(parents=True)
            shutil.copy2(source, target)
        target = state / "app"
        if target.exists():
            backup = state / "backups" / ("app-" + str(time.time_ns()))
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, backup)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def guest(config, args, data=None, timeout=180):
    return run([config["multipass"], "exec", config["instance"], "--", *args],
               data=data, timeout=timeout)


def guest_python(config, source, privileged=False):
    args = ["sudo", "-n", "python3", "-"] if privileged else ["python3", "-"]
    return guest(config, args, data=source)


def guest_host_network(config):
    routes = json.loads(guest(config, ["ip", "-j", "-4", "route", "show", "default"]).stdout)
    route = next((item for item in routes if item.get("gateway") and item.get("dev")), None)
    if not route:
        raise RuntimeError("Cannot find the guest's host gateway")
    address = str(ipaddress.ip_address(route["gateway"]))
    links = json.loads(guest(config, ["ip", "-j", "-4", "address", "show", "dev", route["dev"]]).stdout)
    for link in links:
        for item in link.get("addr_info", []):
            network = ipaddress.ip_interface(f"{item['local']}/{item['prefixlen']}").network
            if ipaddress.ip_address(address) in network:
                return address, str(network)
    raise RuntimeError("Cannot establish the guest subnet for the host SSH key")


def mount_plan(config):
    """Reject conflicts before changing an existing installation."""
    info = json.loads(run([config["multipass"], "info", config["instance"], "--format", "json"]).stdout)["info"][config["instance"]]
    shares = [(config["host_home"], config["host_home"])]
    if config["share_volumes"]:
        shares.append(("/Volumes", "/Volumes"))
    missing = []
    for source, target in shares:
        current = info.get("mounts", {}).get(target)
        if current and current["source_path"] != source:
            raise RuntimeError("A different source is already mounted at " + target)
        if not current:
            missing.append((source, target))
    return missing


def record_transaction(state, transaction, phase, status="incomplete", error=None):
    transaction.update(phase=phase, status=status, updated_at=time.time())
    if error is None:
        transaction.pop("error", None)
    else:
        transaction["error"] = str(error)
    atomic_write(state / "install-transaction.json", json.dumps(transaction, indent=2) + "\n")


def read_host_public_key():
    return public_key(Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text())


def provision_guest(config, state, shell_public, host_public):
    user = config["guest_user"]
    guest_home = config["guest_home"]
    guest_settings = {"host_home": config["host_home"], "host_user": config["host_user"],
                      "host_address": config["host_address"], "host_alias": "o-linux-mac",
                      "share_volumes": config["share_volumes"],
                      "identity": guest_home + "/.ssh/o_linux_host_ed25519",
                      "known_hosts": guest_home + "/.ssh/o_linux_host_known_hosts"}
    payload = {"user": user, "home": guest_home, "host_home": config["host_home"],
               "share_volumes": config["share_volumes"], "shell_public": shell_public,
               "files": {"/usr/local/bin/mac": {
                   "content": base64.b64encode((state / "app/src/olinux/guest_mac.py").read_bytes()).decode(),
                   "mode": 0o755, "owner": "root"},
                   guest_settings["identity"]: {
                       "content": base64.b64encode((state / "host_id_ed25519").read_bytes()).decode(),
                       "mode": 0o600, "owner": user},
                   guest_settings["known_hosts"]: {
                       "content": base64.b64encode(("o-linux-mac " + host_public + "\n").encode()).decode(),
                       "mode": 0o600, "owner": user},
                   "/etc/o-linux/host.json": {
                       "content": base64.b64encode((json.dumps(guest_settings, indent=2) + "\n").encode()).decode(),
                       "mode": 0o644, "owner": "root"}}}
    source = """
import base64,json,os,pathlib,pwd,tempfile,time
p=json.loads(PAYLOAD)
account=pwd.getpwnam(p['user'])
home=pathlib.Path(p['home']); ssh=home/'.ssh'
auth=ssh/'authorized_keys'
if auth.is_symlink(): raise RuntimeError('Refusing a symlinked authorized_keys')
if ssh.is_symlink(): raise RuntimeError('Refusing a symlinked SSH directory')
for name in p['files']:
    path=pathlib.Path(name)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError('Refusing a non-regular destination: '+name)
for name,target in [('home',p['host_home']),('volumes','/Volumes')]:
    path=pathlib.Path('/host')/name
    if path.is_symlink() and os.readlink(path)!=target:
        raise RuntimeError('Existing host shortcut has a different target: '+str(path))
    if path.exists() and not path.is_symlink():
        raise RuntimeError('Existing host shortcut is not a symlink: '+str(path))
ssh.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(ssh,0o700); os.chown(ssh,account.pw_uid,account.pw_gid)
old=auth.read_bytes() if auth.exists() else b''
token=p['shell_public'].split()[1].encode()
if not any(token in line.split() for line in old.splitlines()):
    with auth.open('ab') as f:
        f.write((b'' if not old or old.endswith(b'\\n') else b'\\n')+p['shell_public'].encode()+b'\\n')
os.chmod(auth,0o600); os.chown(auth,account.pw_uid,account.pw_gid)
for name,item in p['files'].items():
    path=pathlib.Path(name); path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink(): raise RuntimeError('Refusing a symlinked destination: '+name)
    content=base64.b64decode(item['content'])
    if path.exists() and path.read_bytes()!=content:
        backup=pathlib.Path('/var/lib/o-linux/backups')/(path.name+'-'+str(time.time_ns()))
        backup.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        backup.write_bytes(path.read_bytes()); backup.chmod(0o600)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    with os.fdopen(fd,'wb') as f: f.write(content)
    owner=pwd.getpwnam(item['owner']); os.chmod(tmp,item['mode']); os.chown(tmp,owner.pw_uid,owner.pw_gid)
    os.replace(tmp,path)
for name in [p['host_home'],'/Volumes','/host']: pathlib.Path(name).mkdir(parents=True,exist_ok=True)
for name,target in [('home',p['host_home']),('volumes','/Volumes')]:
    path=pathlib.Path('/host')/name
    if path.is_symlink():
        if os.readlink(path)==target: continue
        raise RuntimeError('Existing host shortcut has a different target: '+str(path))
    if path.exists(): raise RuntimeError('Existing host shortcut is not a symlink: '+str(path))
    path.symlink_to(target)
print('Guest command paths installed.')
""".replace("PAYLOAD", repr(json.dumps(payload)))
    guest_python(config, source, privileged=True)


def make_config(args, old=None):
    old = old or {}
    instance = args.instance or old.get("instance", "o-linux")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{0,62}", instance):
        raise ValueError("Invalid instance name")
    home = str(Path.home().resolve())
    if any(c in home for c in "\n\r\0"):
        raise ValueError("The host home cannot contain line separators")
    result = dict(old)
    result.update({"schema": 1, "provider": "multipass", "instance": instance, "host_home": home,
              "host_user": getpass.getuser(), "guest_user": "ubuntu", "guest_home": "/home/ubuntu",
              "multipass": old.get("multipass") or shutil.which("multipass"),
              "qemu_nbd": old.get("qemu_nbd") or shutil.which("qemu-nbd"),
              "guest_host_alias": "o-linux-guest", "host_alias": "o-linux-mac",
              "share_volumes": args.share_volumes if args.share_volumes is not None else old.get("share_volumes", True),
              "cpus": args.cpus if args.cpus is not None else old.get("cpus", 4),
              "memory": args.memory if args.memory is not None else old.get("memory", "4G"),
              "disk": args.disk if args.disk is not None else old.get("disk", "16G"),
              "release": args.release if args.release is not None else old.get("release", "26.04")})
    if old:
        if old.get("provider", "multipass") != "multipass":
            raise RuntimeError("Existing configuration uses a different provider; use a separate O_LINUX_STATE")
        for name in ("instance", "host_home", "host_user", "guest_user", "guest_home"):
            if old.get(name) != result[name]:
                raise RuntimeError("Existing configuration differs at " + name + "; use a separate O_LINUX_STATE")
    return result


def main(argv=None):
    if platform.system() == "Linux":
        from .native import main as native_main
        return native_main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance")
    parser.add_argument("--adopt-existing", action="store_true", help="configure the named existing VM without replacing its disk")
    parser.add_argument("--replace-launcher", action="store_true", help="back up and replace existing linux/o-linux commands")
    parser.add_argument("--plan", action="store_true", help="show planned local paths and resources without making changes")
    parser.add_argument("--cpus", type=int)
    parser.add_argument("--memory")
    parser.add_argument("--disk")
    parser.add_argument("--release")
    volumes = parser.add_mutually_exclusive_group()
    volumes.add_argument("--no-volumes", action="store_false", dest="share_volumes", help="do not add a /Volumes mapping; existing mappings are preserved")
    volumes.add_argument("--volumes", action="store_true", dest="share_volumes", help="add a /Volumes mapping")
    parser.set_defaults(share_volumes=None)
    args = parser.parse_args(argv)
    transaction = None
    try:
        state = state_dir()
        existing = state / "config.json"
        old = json.loads(existing.read_text()) if existing.exists() else None
        config = make_config(args, old)
        if config["cpus"] < 1 or not re.fullmatch(r"[1-9][0-9]*(?:[.][0-9]+)?[MG]", config["memory"]) or not re.fullmatch(r"[1-9][0-9]*G", config["disk"]):
            raise ValueError("Use a positive CPU count, memory such as 4G, and disk such as 16G")
        if not config["release"] or config["release"].startswith("-"):
            raise ValueError("Invalid Ubuntu release")
        if args.plan:
            print(json.dumps({"state": str(state), "configuration": config,
                              "launchers": [str(Path.home() / ".local/bin" / n) for n in ("linux", "o-linux")],
                              "adopt_existing": args.adopt_existing, "replace_launcher": args.replace_launcher}, indent=2))
            return 0
        if platform.system() != "Darwin":
            raise RuntimeError("This installer configures macOS. Use the native Linux provider on Linux.")
        if os.geteuid() == 0:
            raise RuntimeError("Run setup as your normal Mac account, not through sudo")
        for tool in ("multipass", "qemu_nbd"):
            if not config[tool]:
                raise RuntimeError(tool.replace("_", "-") + " is required; install Multipass and QEMU first")
        if not shutil.which("ssh-keygen"):
            raise RuntimeError("ssh-keygen is required")
        try:
            with socket.create_connection(("127.0.0.1", 22), timeout=3):
                pass
        except OSError as error:
            raise RuntimeError("Host SSH is unavailable. Enable Remote Login for your account in macOS Settings, then rerun setup.") from error
        try:
            host_public = read_host_public_key()
        except OSError as error:
            raise RuntimeError("The host Ed25519 SSH public key is unavailable; check the Remote Login setup") from error
        launcher = launcher_text(state)
        targets = [Path.home() / ".local/bin" / n for n in ("linux", "o-linux")]
        for path in targets:
            check_launcher(path, launcher, args.replace_launcher)
        rows = json.loads(run([config["multipass"], "list", "--format", "json"]).stdout)["list"]
        selected = next((row for row in rows if row["name"] == config["instance"]), None)
        if selected and not old and not args.adopt_existing:
            raise RuntimeError("The instance exists; use --adopt-existing to configure it deliberately")
        if selected:
            mount_plan(config)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (state / "install.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            repository = Path(__file__).resolve().parents[2]
            retry = [sys.executable, str(repository / "scripts/install.py"), "--instance", config["instance"],
                     "--adopt-existing", "--replace-launcher", "--cpus", str(config["cpus"]),
                     "--memory", config["memory"], "--disk", config["disk"], "--release", config["release"]]
            if not config["share_volumes"]:
                retry.append("--no-volumes")
            transaction = {"schema": 1, "instance": config["instance"], "state": str(state),
                           "started_at": time.time(), "retry_argv": retry,
                           "retry_environment": {"O_LINUX_STATE": str(state)},
                           "recovery": "Rerun retry_argv with retry_environment. Setup preserves keys and unrelated authorized_keys entries; no VM disk is replaced. Local and guest backups are retained."}
            record_transaction(state, transaction, "guest-start")
            if not selected:
                cloud = state / "cloud-init.yaml"
                atomic_write(cloud, cloud_config(config))
                print("Creating " + config["instance"] + "...", flush=True)
                run([config["multipass"], "launch", config["release"], "--name", config["instance"],
                     "--cpus", str(config["cpus"]), "--memory", config["memory"], "--disk", config["disk"],
                     "--cloud-init", str(cloud)], timeout=900)
            elif selected["state"] != "Running":
                if selected["state"] not in ("Stopped", "Suspended"):
                    raise RuntimeError("The selected VM is in state " + selected["state"])
                run([config["multipass"], "start", config["instance"]], timeout=180)
            shares = mount_plan(config)
            record_transaction(state, transaction, "guest-readiness")
            guest(config, ["cloud-init", "status", "--wait"], timeout=600)
            # Adopted guests must have the storage prerequisites used by the interface.
            check = guest_python(config, "import shutil\nfor name in ['python3','nbd-client','ddrescue']:\n print(shutil.which(name) or '')\n")
            if len(check.stdout.splitlines()) != 3 or not all(check.stdout.splitlines()):
                raise RuntimeError("The adopted VM needs python3, nbd-client and gddrescue installed before setup")
            config["host_address"], config["host_subnet"] = guest_host_network(config)
            guest_public = public_key(guest(config, ["cat", "/etc/ssh/ssh_host_ed25519_key.pub"]).stdout)
            record_transaction(state, transaction, "credentials")
            pin_host_key(state / "guest_known_hosts", "o-linux-guest", guest_public)
            pin_host_key(state / "host_known_hosts", "o-linux-mac", host_public)
            shell_public = ensure_key(state / "guest_id_ed25519")
            bridge_public = ensure_key(state / "host_id_ed25519")
            record_transaction(state, transaction, "local-app-and-config")
            install_app(repository, state)
            atomic_write(existing, json.dumps(config, indent=2) + "\n")
            record_transaction(state, transaction, "host-authorization")
            host_command = shlex.join([sys.executable, str(state / "app/src/olinux/host_bridge.py"), "--config", str(existing)])
            escaped = host_command.replace("\\", "\\\\").replace('"', '\\"')
            entry = (f'restrict,pty,from="127.0.0.1,::1,{config["host_subnet"]}",'
                     f'command="{escaped}" ' + bridge_public + " o-linux-host")
            add_host_key(Path(config["host_home"]) / ".ssh/authorized_keys", bridge_public, entry, state)
            record_transaction(state, transaction, "guest-provision")
            provision_guest(config, state, shell_public, host_public)
            record_transaction(state, transaction, "shared-directories")
            for source, target in shares:
                run([config["multipass"], "mount", source, config["instance"] + ":" + target], timeout=180)
            record_transaction(state, transaction, "bridge-verification")
            answer = guest(config, ["/usr/local/bin/mac", "uname", "-s"]).stdout.strip()
            if answer != "Darwin":
                raise RuntimeError("Guest-to-host command verification failed")
            record_transaction(state, transaction, "launchers")
            for path in targets:
                install_launcher(path, launcher, state, args.replace_launcher)
            record_transaction(state, transaction, "complete", status="complete")
            print("Installed. Run linux for a shell, or linux COMMAND. Inside Linux, mac COMMAND uses the host.")
            return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
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
