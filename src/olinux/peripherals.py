"""Read peripheral metadata through native host tools."""
import glob
import platform
import subprocess


COMMANDS = {
    "disks": ["/usr/sbin/diskutil", "list", "external"],
    "usb": ["/usr/sbin/ioreg", "-p", "IOUSB", "-w", "0"],
    "thunderbolt": ["/usr/sbin/system_profiler", "SPThunderboltDataType"],
    "bluetooth": ["/usr/sbin/system_profiler", "SPBluetoothDataType"],
    "audio": ["/usr/sbin/system_profiler", "SPAudioDataType"],
    "cameras": ["/usr/sbin/system_profiler", "SPCameraDataType"],
    "displays": ["/usr/sbin/system_profiler", "SPDisplaysDataType"],
    "network": ["/usr/sbin/networksetup", "-listallhardwareports"],
    "printers": ["/usr/bin/lpstat", "-p", "-d"],
}
CATEGORIES = (*COMMANDS, "serial")
LINUX_COMMANDS = {
    "disks": ["lsblk", "--json", "--output", "NAME,PATH,TYPE,SIZE,FSTYPE,RO,MOUNTPOINTS"],
    "usb": ["lsusb"],
    "thunderbolt": ["ls", "-1", "/sys/bus/thunderbolt/devices"],
    "bluetooth": ["bluetoothctl", "list"],
    "audio": ["cat", "/proc/asound/cards"],
    "cameras": ["v4l2-ctl", "--list-devices"],
    "displays": ["xrandr", "--query"],
    "network": ["ip", "-json", "address", "show"],
    "printers": ["lpstat", "-p", "-d"],
}


def inventory(category="all", timeout=30):
    """Return metadata only; never capture, print, mount or repair anything."""
    if category != "all" and category not in CATEGORIES:
        raise ValueError("Unknown peripheral category: " + str(category))
    system = platform.system()
    if system not in ("Darwin", "Linux"):
        raise RuntimeError("Peripheral inventory supports macOS and Linux")
    commands = COMMANDS if system == "Darwin" else LINUX_COMMANDS
    selected = CATEGORIES if category == "all" else (category,)
    results = {}
    for name in selected:
        if name == "serial":
            patterns = ("/dev/cu.*",) if system == "Darwin" else (
                "/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*")
            paths = {path for pattern in patterns for path in glob.glob(pattern)}
            results[name] = {"paths": sorted(paths), "ports_opened": False}
            continue
        argv = list(commands[name])
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            results[name] = {"command": argv, "exit_code": result.returncode,
                             "stdout": result.stdout, "stderr": result.stderr}
        except subprocess.TimeoutExpired:
            results[name] = {"command": argv, "error": f"Metadata query timed out after {timeout} seconds"}
        except OSError as error:
            results[name] = {"command": argv, "error": str(error)}
    return results
