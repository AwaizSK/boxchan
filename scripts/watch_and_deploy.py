#!/usr/bin/env python3
"""Watch repository targets and auto-deploy discord-tts-bot on changes."""

import argparse
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

DEFAULT_SRC = Path(__file__).resolve().parent.parent
if not (DEFAULT_SRC / ".git").is_dir():
    DEFAULT_SRC = Path("/opt/workspace/boxchan/discord-tts-bot")

IGNORED_DIRS = {".git", "__pycache__", "tests", "docs"}
IGNORED_FILENAMES = {"state.json", "config.json"}
COOLDOWN_SECONDS = 5.0

_running = True


def handle_signal(signum, _frame):
    global _running
    sig_name = signal.Signals(signum).name
    logging.info("Received %s, stopping...", sig_name)
    _running = False


def should_ignore(path: Path, src: Path) -> bool:
    try:
        rel = path.relative_to(src)
    except ValueError:
        return True

    for part in rel.parts:
        if part in IGNORED_DIRS:
            return True

    name = path.name
    if name.startswith("."):
        return True
    if name in IGNORED_FILENAMES:
        return True
    if name.endswith((".swp", ".swo", "~", ".bak", ".tmp")):
        return True
    if "repomix" in name:
        return True

    return False


def scan_tracked_files(src: Path) -> dict[Path, tuple[int, int]]:
    files: set[Path] = set()

    # Track SRC/*.py
    for p in src.glob("*.py"):
        if p.is_file() and not p.is_symlink() and not should_ignore(p, src):
            files.add(p)

    # Track SRC/requirements.txt
    req = src / "requirements.txt"
    if req.is_file() and not req.is_symlink() and not should_ignore(req, src):
        files.add(req)

    # Track SRC/static/* (flat: regular files)
    static_dir = src / "static"
    if static_dir.is_dir() and not static_dir.is_symlink():
        for p in static_dir.glob("*"):
            if p.is_file() and not p.is_symlink() and not should_ignore(p, src):
                files.add(p)

    # Track SRC/models/*.onnx, SRC/models/*.onnx.json
    models_dir = src / "models"
    if models_dir.is_dir() and not models_dir.is_symlink():
        for pat in ("*.onnx", "*.onnx.json"):
            for p in models_dir.glob(pat):
                if p.is_file() and not p.is_symlink() and not should_ignore(p, src):
                    files.add(p)

    snapshot: dict[Path, tuple[int, int]] = {}
    for p in files:
        try:
            st = p.stat()
            snapshot[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return snapshot


def validate_python_files(src: Path) -> bool:
    py_files = [
        p for p in sorted(src.glob("*.py"))
        if p.is_file() and not p.is_symlink() and not should_ignore(p, src)
    ]
    if not py_files:
        logging.warning("Pre-deploy validation failed: no Python files found in %s", src)
        return False

    if not (src / "bot.py").is_file():
        logging.warning("Pre-deploy validation failure: bot.py missing in %s", src)
        return False

    all_ok = True
    for py_file in py_files:
        try:
            source = py_file.read_bytes()
            compile(source, str(py_file), "exec")
        except SyntaxError as e:
            logging.warning("Pre-deploy validation syntax error in %s: %s", py_file.name, e)
            all_ok = False
        except Exception as e:
            logging.warning("Pre-deploy validation failure in %s: %s", py_file.name, e)
            all_ok = False
    return all_ok


def run_deploy(src: Path) -> int:
    installed_deploy = Path("/opt/tts-bot/scripts/deploy.sh")
    repo_deploy = src / "scripts" / "deploy.sh"

    if installed_deploy.is_file() and os.geteuid() == 0:
        deploy_script = installed_deploy
        cmd = ["bash", str(deploy_script), str(src)]
    elif repo_deploy.is_file():
        deploy_script = repo_deploy
        cmd = ["bash", str(deploy_script)] if os.geteuid() == 0 else ["sudo", "bash", str(deploy_script)]
    else:
        logging.error("Deploy script missing: %s (and %s)", installed_deploy, repo_deploy)
        return 1

    logging.info("Executing deploy: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(src),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=120,
        )
        if proc.stdout:
            for line in proc.stdout.strip().splitlines():
                logging.info("[deploy] %s", line)
        if proc.returncode == 0:
            logging.info("Deploy completed successfully.")
        else:
            logging.error("Deploy failed with exit code %d", proc.returncode)
        return proc.returncode
    except subprocess.TimeoutExpired:
        logging.error("Deploy timed out after 120s")
        return 1
    except Exception as e:
        logging.error("Failed to run deploy: %s", e)
        return 1


def interruptible_sleep(duration: float) -> bool:
    deadline = time.time() + duration
    while _running and time.time() < deadline:
        time.sleep(min(0.2, max(0.0, deadline - time.time())))
    return _running


def main():
    parser = argparse.ArgumentParser(description="Watch repository targets and auto-deploy discord-tts-bot.")
    parser.add_argument(
        "--src",
        type=Path,
        default=DEFAULT_SRC,
        help="Path to repository root to watch (default: %(default)s)",
    )
    parser.add_argument("--interval", type=float, default=1.5, help="Poll interval in seconds (default: 1.5)")
    parser.add_argument("--debounce", type=float, default=2.0, help="Debounce duration in seconds (default: 2.0)")
    parser.add_argument("--once", action="store_true", help="Run validation and deploy once, then exit")
    args = parser.parse_args()

    src = args.src.resolve()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.once:
        logging.info("Running single deploy cycle (--once)...")
        if not validate_python_files(src):
            logging.warning("Pre-deploy validation failed; aborting deploy.")
            sys.exit(1)
        rc = run_deploy(src)
        sys.exit(rc)

    interval = max(0.1, args.interval)
    debounce = max(0.0, args.debounce)
    last_deploy_time = 0.0

    current_state = scan_tracked_files(src)
    logging.info("Watching %s (%d files tracked, interval=%.1fs, debounce=%.1fs)...", src, len(current_state), interval, debounce)

    while _running:
        if not interruptible_sleep(interval):
            break

        new_state = scan_tracked_files(src)
        if new_state == current_state:
            continue

        changed_names = []
        for p in new_state:
            if p not in current_state:
                changed_names.append(f"+{p.relative_to(src)}")
            elif new_state[p] != current_state[p]:
                changed_names.append(f"~{p.relative_to(src)}")
        for p in current_state:
            if p not in new_state:
                changed_names.append(f"-{p.relative_to(src)}")

        logging.info("Change detected (%s). Debouncing for %.1fs...", ", ".join(changed_names[:5]), debounce)

        settled = False
        while _running and not settled:
            if not interruptible_sleep(debounce):
                break
            latest_state = scan_tracked_files(src)
            if latest_state == new_state:
                settled = True
            else:
                logging.debug("Additional changes detected during debounce, extending...")
                new_state = latest_state

        if not _running:
            break

        pre_deploy_state = new_state

        # Cooldown check: min 5s between deploys
        elapsed = time.time() - last_deploy_time
        if elapsed < COOLDOWN_SECONDS:
            remaining = COOLDOWN_SECONDS - elapsed
            logging.info("Deploy cooldown: waiting %.1fs...", remaining)
            if not interruptible_sleep(remaining):
                break

        if not _running:
            break

        logging.info("Validating Python files before deploy...")
        if not validate_python_files(src):
            logging.warning("Pre-deploy validation failed; skipping deploy without crashing.")
            current_state = pre_deploy_state
            continue

        run_deploy(src)
        last_deploy_time = time.time()
        current_state = pre_deploy_state

    logging.info("Watcher stopped.")


if __name__ == "__main__":
    main()
