"""Ostadix-owned execution documents and native kernel command routes."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import uuid

from .config import load_config, state_dir
from .providers import execute, effective_cwd


def environment(config):
    explicit = os.environ.get("O_LANG_ROOT") or config.get("ostadix_root")
    candidates = [explicit] if explicit else [str(Path.home() / "ostadix-BM"), str(Path.home() / "OSTADIX")]
    root = next((Path(p).expanduser().resolve() for p in candidates if p and (Path(p).expanduser() / "backends").is_dir()), None)
    if root is None:
        raise RuntimeError("Set O_LANG_ROOT or ostadix_root to an installed OSTADIX checkout")
    def find(name):
        # O and o collide on case-insensitive filesystems; use distinct native
        # artifact names before the historical public aliases.
        artifact = {"o": "o-cli", "O": "ostadix-evaluator"}.get(name, name)
        p = shutil.which(artifact)
        if p: return p
        artifact = "O" if name == "O" else artifact
        for p in (root / "target/release" / artifact, root / "target/debug" / artifact):
            if p.is_file() and os.access(p, os.X_OK): return str(p)
        if platform.system() != "Darwin":
            p = shutil.which(name)
            if p: return p
        raise RuntimeError("Missing OSTADIX executable: " + name)
    env = os.environ.copy()
    env.update(O_LANG_ROOT=str(root), O_BACKENDS_DIR=str(root / "backends"), O_LINUX_STATE=str(state_dir()))
    return root, find, env


def native_argv(config, route, arguments):
    root, find, env = environment(config)
    if route == "kernel":
        # Preserve the repository-owned build, boot and physical-media contracts.
        argv = [find("o"), "tool", "kernel", *(arguments or ["help"])]
        env.setdefault("OCORE_BUILD_DIR", str(state_dir() / "kernel-build"))
    elif route == "systems":
        argv = [find("o"), "tool", "foreign-kernel-lab", *(arguments or ["list"])]
        if any(a in ("run", "run-all") for a in arguments) and not any(a == "--output-dir" or a.startswith("--output-dir=") for a in arguments):
            argv += ["--output-dir", str(state_dir() / "foreign-system-runs")]
    else:
        argv = [find("o"), *arguments]
    return argv, env


def source_for(payload):
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    package = base64.b64encode(str(Path(__file__).resolve().parents[1]).encode()).decode()
    return ("python^(\nimport base64, sys\n"
            f"sys.path.insert(0, base64.b64decode('{package}').decode())\n"
            "from olinux.ostadix import execute_payload\n"
            f"__oval_result__ = execute_payload('{encoded}')\n)_python\n")


def execute_payload(encoded):
    payload = json.loads(base64.b64decode(encoded))
    config = load_config()
    started = time.time()
    try:
        result = execute(config, payload["target"], payload["argv"], payload["cwd"], payload.get("timeout"))
        record = dict(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr, timed_out=False)
    except subprocess.TimeoutExpired as error:
        def text(value): return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
        record = dict(exit_code=124, stdout=text(error.stdout), stderr=text(error.stderr), timed_out=True,
                      completion="unknown: remote effects may continue after client timeout")
    record.update(target=payload["target"], argv=payload["argv"], requested_cwd=payload["cwd"],
                  cwd=effective_cwd(config, payload["target"], payload["cwd"]), elapsed_seconds=time.time()-started)
    Path(payload["result_path"]).write_text(json.dumps(record, indent=2) + "\n")
    return record


def job(arguments):
    parser = argparse.ArgumentParser(prog="o-linux job", description="Plan and execute an exact O document targeting the host or Linux")
    parser.add_argument("--target", choices=("linux", "host"), default="linux")
    parser.add_argument("--plan", action="store_true", help="write and plan the document without running its command")
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(arguments)
    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not argv: parser.error("a command is required after --")
    config = load_config()
    root, find, env = environment(config)
    directory = state_dir() / "runs" / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10])
    directory.mkdir(parents=True, mode=0o700)
    source = directory / "source.O"
    result_path = directory / "result.json"
    payload = dict(target=args.target, argv=argv, cwd=os.getcwd(), timeout=args.timeout, result_path=str(result_path))
    source.write_text(source_for(payload))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt = dict(schema="o-linux.run/v1", source_sha256=digest, source=str(source), ostadix_root=str(root), target=args.target,
                   state="created", result=str(result_path))
    receipt_path = directory / "receipt.json"
    def save(): receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    save()
    check = subprocess.run([find("O"), "--check", str(source), str(root / "backends")], env=env, capture_output=True, text=True)
    (directory / "check.log").write_text(check.stdout + check.stderr)
    if check.returncode:
        receipt["state"] = "check-failed"; save()
        raise RuntimeError("Ostadix source check failed: " + str(directory / "check.log"))
    plan = subprocess.run([find("o"), "plan", str(source), "--json", "--execution-intent-json"], env=env, capture_output=True, text=True)
    (directory / "plan.json").write_text(plan.stdout)
    (directory / "plan.stderr").write_text(plan.stderr)
    if plan.returncode:
        receipt["state"] = "plan-failed"; save()
        raise RuntimeError("Ostadix planning failed: " + str(directory / "plan.stderr"))
    try:
        intent = json.loads(plan.stdout)["execution_intent"]
        intent_digest = intent["execution_intent_sha256"]
        if intent["source_sha256"] != digest or len(intent_digest) != 64 or any(c not in "0123456789abcdef" for c in intent_digest):
            raise ValueError("The plan did not bind this source and execution intent")
    except (KeyError, TypeError, ValueError) as error:
        receipt["state"] = "intent-invalid"; save()
        raise RuntimeError("Invalid Ostadix intent plan; command was not dispatched") from error
    receipt.update(state="planned", execution_intent_sha256=intent_digest); save()
    if args.plan:
        print(str(receipt_path)); return 0
    receipt["state"] = "executing"; save()
    result = subprocess.run([find("O"), "--json", "--require-source-sha256", digest,
                             "--require-execution-intent-sha256", intent_digest,
                             str(source), str(root / "backends")], env=env, capture_output=True, text=True)
    (directory / "execution.json").write_text(result.stdout)
    (directory / "execution.stderr").write_text(result.stderr)
    if not result_path.is_file():
        receipt["state"] = "execution-failed-or-unknown"; save()
        raise RuntimeError("Ostadix did not return a command result; inspect " + str(receipt_path) + "; do not retry unknown effects automatically")
    output = json.loads(result_path.read_text())
    status = "completion-unknown" if output.get("timed_out") else ("completed" if result.returncode == 0 else "execution-error")
    receipt.update(state=status, exit_code=output["exit_code"], runtime_exit_code=result.returncode,
                   completion=output.get("completion", "command returned"))
    save()
    sys.stdout.write(output["stdout"]); sys.stderr.write(output["stderr"])
    print("Receipt: " + str(receipt_path), file=sys.stderr)
    return output["exit_code"] if result.returncode == 0 else result.returncode
