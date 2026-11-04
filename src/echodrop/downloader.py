import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from src.echodrop.config import Config
from src.echodrop.cookies import yt_dlp_cookie_args

logger = logging.getLogger(__name__)


def yt_dlp_path() -> Path | None:
    """Return the path to the yt-dlp executable."""
    path = shutil.which("yt-dlp")
    if path:
        return Path(path).resolve()
    return None


def run_yt_dlp(args: list[str], timeout: int | None = None, progress_callback: Callable | None = None) -> tuple[str, str]:
    """Run yt-dlp with the given arguments and return stdout and stderr."""
    executable = yt_dlp_path()
    if not executable:
        msg = "yt-dlp executable not found. Please install yt-dlp and ensure it is in your PATH."
        raise RuntimeError(msg)

    command = [str(executable), *yt_dlp_cookie_args(), *args]

    logger.debug("Running yt-dlp command: %s", " ".join(command))

    environment = os.environ.copy()
    if progress_callback:
        process = subprocess.Popen(
            command, cwd=Config.root(), env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        stdout_lines = []

        if process.stdout is None:
            msg = "Failed to capture stdout from yt-dlp process."
            raise RuntimeError(msg)

        for line in process.stdout:
            stdout_lines.append(line)
            match = re.search(r"\[download\]\s+([\d.]+)%.*?at\s+(\S+/s)", line)
            if match:
                progress_callback(float(match.group(1)), match.group(2))

        stderr = process.stderr.read() if process.stderr else ""
        process.wait(timeout=timeout)
        completed = subprocess.CompletedProcess(command, process.returncode, "".join(stdout_lines), stderr)
    else:
        completed = subprocess.run(
            command, cwd=Config.root(), env=environment, capture_output=True,
            text=True, timeout=timeout, check=False,
        )

    if completed.returncode != 0:
        inner_msg = completed.stderr.strip().splitlines()[-1]
        msg = f"yt-dlp failed with exit code {completed.returncode}: {inner_msg}"
        raise RuntimeError(msg)

    return completed.stdout, completed.stderr


def run_ffmpeg(args: list[str], duration: float | None, progress_callback: Callable | None=None) -> subprocess.CompletedProcess:
    """Run ffmpeg with the given arguments and return a CompletedProcess object."""
    command = [*args[:-1], "-progress", "pipe:1", "-nostats", args[-1]]
    logger.debug("Running ffmpeg command: %s", " ".join(command))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    progress: dict[str, str] = {}

    if process.stdout is None:
        msg = "Failed to capture stdout from ffmpeg process."
        raise RuntimeError(msg)

    for line in process.stdout:
        key, _, value = line.strip().partition("=")
        if not key:
            continue
        progress[key] = value
        if key == "progress" and progress_callback:
            elapsed = int(progress.get("out_time_ms", "0")) / 1_000_000
            percent = min(100, elapsed / duration * 100) if duration and duration > 0 else None
            progress_callback(percent, progress.get("speed", ""))

    stderr = process.stderr.read() if process.stderr else ""
    process.wait()
    return subprocess.CompletedProcess(command, process.returncode, "", stderr)


def ffprobe_duration(path: Path) -> float:
    """Return the duration of a media file in seconds using ffprobe."""
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return float(completed.stdout.strip())

