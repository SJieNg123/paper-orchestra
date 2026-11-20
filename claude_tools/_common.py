"""Shared helpers for the Claude Code tool wrappers.

Conventions every subcommand follows so Claude Code can drive them reliably:
  * Progress logs (including everything upstream code prints) go to stderr.
  * Exactly one JSON object is printed to stdout at the end of a run.
  * Exit code 0 on success, 1 on a handled failure (the JSON then has "error").
"""

import contextlib
import datetime
import io
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(os.path.join(REPO_ROOT, ".env"))
except ImportError:  # pragma: no cover
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@contextlib.contextmanager
def stdout_to_stderr():
    """Route upstream print() calls to stderr so stdout stays clean JSON."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


@contextlib.contextmanager
def silenced_stdout():
    """Swallow stdout entirely (used around imports that print secrets)."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


def emit(result: dict, exit_code: int = 0) -> int:
    # Called only outside stdout_to_stderr() blocks, so sys.stdout is the real one.
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return exit_code


def default_cutoff() -> str:
    return datetime.datetime.now().strftime("%Y-%m")


def read_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def gemini_configured() -> bool:
    return bool(
        os.getenv("GEMINI_API_KEY")
        or (os.getenv("VERTEX_AI_PROJECT") and os.getenv("VERTEX_AI_LOCATION"))
    )
