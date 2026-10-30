"""A contained O declaration followed by one explicit native read-only operation."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import time
from types import SimpleNamespace

from . import drives
from .config import load_config, state_dir
from .install import atomic_write
from .ostadix import environment

SCHEMA = "o-linux.drive-request/v1"
NONCE_ENV = "O_LINUX_DRIVE_NONCE"


def selection_hash(selection):
    return hashlib.sha256(json.dumps(selection, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode()).hexdigest()


def request_for(selection, nonce):
    return {"schema": SCHEMA, "action": "open-readonly", "installation": str(state_dir()),
            "selection_sha256": selection_hash(selection), "nonce": nonce}


def open_request():
    """Metadata only: the O backend never runs sudo or opens a block device."""
    nonce = os.environ.get(NONCE_ENV)
    if nonce is not None and not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise RuntimeError("Invalid native drive-request nonce")
    return request_for(drives.load_record()["selection"], nonce)


def decode_request(value):
    """Decode only the map-of-UTF-8-text OValue shape this request needs."""
    if not isinstance(value, dict) or set(value) != {"t", "v"} or value["t"] != "map" or not isinstance(value["v"], dict):
        raise ValueError("Expected a typed O map")
    result = {}
    for key, item in value["v"].items():
        if not isinstance(item, dict) or set(item) != {"t", "v"} or item["t"] != "text":
            raise ValueError("Drive request fields must be O text values")
        text = item["v"]
        if not isinstance(text, dict) or set(text) != {"utf8", "encoding"} or text["encoding"] != "utf-8" or not isinstance(text["utf8"], str):
            raise ValueError("Invalid O text encoding")
        result[key] = text["utf8"]
    return result


def run_open():
    """Validate the fixed O request; keep host authority in this native caller."""
    if platform.system() != "Darwin":
        raise RuntimeError("The saved-drive reader currently requires macOS and anylinuxfs")
    root, find, env = environment(load_config())
    document = state_dir() / "app/integrations/ostadix/read-drive.O"
    if not document.is_file() or document.is_symlink():
        raise RuntimeError("The installed drive declaration is missing or not a regular file; rerun setup")
    digest = hashlib.sha256(document.read_bytes()).hexdigest()
    with drives.drive_lock():
        selection = drives.load_record()["selection"]
        nonce = secrets.token_hex(32)
        expected = request_for(selection, nonce)
        directory = state_dir() / "runs" / ("drive-" + time.strftime("%Y%m%dT%H%M%S") + "-" + nonce[:10])
        directory.mkdir(mode=0o700, parents=True)
        receipt_path = directory / "receipt.json"
        receipt = {"schema": "o-linux.drive-operation/v1", "state": "declaring", "request": expected,
                   "document": str(document), "document_sha256": digest,
                   "authority": "native-caller; fixed-read-only-operation"}
        def save():
            atomic_write(receipt_path, json.dumps(receipt, indent=2) + "\n")
        save()
        env[NONCE_ENV] = nonce
        native_dispatched = False
        try:
            result = subprocess.run([find("O"), "--json", str(document), str(root / "backends")],
                                    env=env, capture_output=True, text=True, timeout=30)
            (directory / "declaration.json").write_text(result.stdout)
            (directory / "declaration.stderr").write_text(result.stderr)
            if result.returncode:
                raise RuntimeError("O drive declaration failed; no native operation dispatched; see " + str(directory))
            try:
                declaration = json.loads(result.stdout)
                request = decode_request(declaration["value"])
                valid = (declaration.get("ok") is True and isinstance(request, dict)
                         and set(request) == set(expected) and request == expected)
            except (ValueError, KeyError, TypeError, AttributeError):
                valid = False
            if not valid:
                raise RuntimeError("O returned an invalid or stale drive request; no native operation dispatched")
            if hashlib.sha256(document.read_bytes()).hexdigest() != digest:
                raise RuntimeError("The drive declaration changed during evaluation; no native operation dispatched")
            if request_for(drives.load_record()["selection"], nonce) != expected:
                raise RuntimeError("The saved drive selection changed during evaluation; no native operation dispatched")
            receipt["state"] = "native-operation"; save()
            # Do not use request-supplied argv, paths, options or privileges.
            # operate re-reads the selected profile and verifies physical and
            # mounted identities. This caller already holds the profile lock.
            native_dispatched = True
            opened = drives.operate(SimpleNamespace(action="open"))
            receipt.update(state="completed", result=opened); save()
            return {**opened, "operation_receipt": str(receipt_path)}
        except BaseException as error:
            receipt.update(state="native-completion-unknown" if native_dispatched else "declaration-rejected",
                           error=str(error))
            try:
                save()
            except OSError:
                # The last durable native-operation state already means that
                # completion needs reconciliation; do not mask the failure.
                pass
            raise
