import json
import logging
import subprocess
import tempfile
import threading
import urllib.parse
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, Self

from src.echodrop import resolver
from src.echodrop.config import Config
from src.echodrop.downloader import run_ffmpeg, run_yt_dlp, yt_dlp_path

logger = logging.getLogger(__name__)

@dataclass
class Job:
    """Data class to hold download job information."""

    id: str
    title: str
    status: str
    progress: float | None = None
    speed: str = ""


class JobStore:
    """Thread-safe, singleton class to manage download jobs."""

    _instance: ClassVar[Self | None] = None
    _lock: threading.Lock
    _jobs: dict[str, Job]

    def __new__(cls, *args, **kwargs) -> Self:  # noqa: ANN002, ANN003, ARG004
        """Create a singleton instance of JobStore."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._lock = threading.Lock()
            cls._instance._jobs = {}
        return cls._instance

    @classmethod
    def get_jobs(cls) -> dict[str, Job]:
        """Return a copy of the jobs dictionary."""
        with cls()._lock:
            return dict(cls()._jobs)

    @classmethod
    def add_job(cls, job_id: str, job: Job) -> None:
        """Set a job in the jobs dictionary."""
        with cls()._lock:
            cls()._jobs[job_id] = job

    @classmethod
    def update_status(cls, job_id: str, status: str) -> None:
        """Update the status of a job in the jobs dictionary."""
        with cls()._lock:
            if job_id in cls()._jobs:
                cls()._jobs[job_id].status = status

    @classmethod
    def update_progress(cls, job_id: str, progress: float | None, speed: str, phase: str) -> None:
        """Update the progress and speed of a job in the jobs dictionary."""
        with cls()._lock:
            if job_id in cls()._jobs:
                cls()._jobs[job_id].status = phase
                if progress is not None:
                    cls()._jobs[job_id].progress = round(progress, 1)
                if speed:
                    cls()._jobs[job_id].speed = speed

    @classmethod
    def has_job(cls, job_id: str) -> bool:
        """Check if a job exists in the jobs dictionary."""
        with cls()._lock:
            return job_id in cls()._jobs

    @classmethod
    def remove_job(cls, job_id: str) -> None:
        """Remove a job from the jobs dictionary."""
        with cls()._lock:
            if job_id in cls()._jobs:
                del cls()._jobs[job_id]


class EchoDropRequestHandler(BaseHTTPRequestHandler):
    """Application request handler."""

    def send_json(self, data: dict, status: HTTPStatus) -> None:
        """Send a JSON response to the client."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        """Read and parse JSON data from the request body."""
        content_length = int(self.headers.get("Content-Length", 0))

        if content_length > Config.data().max_request_size:
            msg = "Request body too large"
            raise ValueError(msg)

        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def do_GET(self) -> None:
        """Handle GET requests."""
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/status":
            self.send_json({
                "configured": True,
                "ytDlpInstalled": bool(yt_dlp_path()),
                "ytDlpPath": str(yt_dlp_path()),
                "downloadDir": str(Config.download_dir()),
            }, HTTPStatus.OK)
            return

        if parsed.path == "/api/jobs":
            jobs = JobStore.get_jobs()
            self.send_json(
                {"jobs": [vars(job) for job in jobs.values()]}, HTTPStatus.OK,
            )
            return

        # Serve static files
        relative = urllib.parse.unquote(parsed.path.lstrip("/")) or "index.html"
        candidate = (Config.root() / relative).resolve()
        if Config.root() not in candidate.parents and candidate != Config.root():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = "text/html; charset=utf-8" if candidate.suffix == ".html" else "text/css; charset=utf-8" if candidate.suffix == ".css" else "application/javascript; charset=utf-8"
        body = candidate.read_bytes()

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """Handle POST requests."""
        parsed = urllib.parse.urlparse(self.path)

        handlers = {
            "/api/resolve": self.do_resolve_recording,
            "/api/resolve-section": self.do_resolve_section,
            "/api/resolve-lesson": self.do_resolve_lesson,
            "/api/download": self.do_start_download,
        }

        try:
            data = self.read_json()
            if parsed.path in handlers:
                handlers[parsed.path](data)
            else:
                self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)

    def do_resolve_recording(self, data: dict) -> None:
        """Handle the /api/resolve endpoint to resolve a recording URL."""
        recording_url = str(data.get("url", "")).strip()
        parsed = urllib.parse.urlparse(recording_url)
        if parsed.scheme not in ("http", "https"):
            msg = "Invalid URL scheme. Only 'http' and 'https' are allowed."
            raise ValueError(msg)

        if "echo360.org" in parsed.netloc:
            data = vars(resolver.resolve_lesson(recording_url))
        else:
            output, _ = run_yt_dlp(["--dump-single-json", "--no-warnings", "--skip-download", recording_url])
            data = json.loads(output)

        is_formats_vald = "formats" in data and data["formats"] and str(data["formats"][0].get("formatId", "")).startswith("echo-")
        if not is_formats_vald:
            formats = [
                {
                    "formatId": item.get("format_id"),
                    "type": "Audio" if item.get("vcodec") == "none" else "Video",
                    "quality": item.get("format_note") or item.get("resolution") or item.get("acodec") or "Source",
                    "size": item.get("filesize") or item.get("filesize_approx"),
                    "url": item.get("url"),
                }
                for item in data.get("formats", [])
                if item.get("url") and (item.get("vcodec") == "none" or item.get("acodec") == "none" or item.get("vcodec"))
            ]
            data["formats"] = formats

        self.send_json(data, HTTPStatus.OK)

    def do_resolve_section(self, data: dict) -> None:
        """Handle the /api/resolve-section endpoint to resolve a section URL."""
        section_url = str(data.get("url", "")).strip()
        parsed = urllib.parse.urlparse(section_url)

        if parsed.scheme not in ("http", "https"):
            msg = "Invalid URL scheme. Only 'http' and 'https' are allowed."
            raise ValueError(msg)

        section_data = resolver.resolve_section(section_url)
        self.send_json({"lectures": section_data}, HTTPStatus.OK)

    def do_resolve_lesson(self, data: dict) -> None:
        """Handle the /api/resolve-lesson endpoint to resolve a lesson URL."""
        url = str(data.get("url") or data.get("lessonUrl") or "").strip()
        parsed = urllib.parse.urlparse(url)
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme not in {"http", "https"}
            or "echo360.org" not in parsed.netloc
            or "lesson" not in path_parts
            or path_parts.index("lesson") + 1 >= len(path_parts)
        ):
            msg = "Invalid Echo360 lesson URL."
            raise ValueError(msg)
        lesson_data = resolver.resolve_lesson(url)
        self.send_json(vars(lesson_data), HTTPStatus.OK)

    def do_start_download(self, data: dict) -> None:
        """Handle the /api/download endpoint to start a download job."""
        url = str(data.get("url", "")).strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            msg = "Invalid URL scheme."
            raise ValueError(msg)

        Config.download_dir().mkdir(mode=0o755, parents=True, exist_ok=True)
        title = str(data.get("title", "")).strip() or "echo360-recording"
        safe_title = "".join(character if character.isalnum() or character in " ._-" else "_" for character in title).strip(" .")
        output_path = str(Config.download_dir() / f"{safe_title}.mp4")
        video_url = str(data.get("videoUrl", "")).strip()
        audio_url = str(data.get("audioUrl", "")).strip()
        if video_url or audio_url:
            thread_args = (video_url, audio_url, output_path)
        else:
            video_id = str(data.get("videoFormatId", "")).strip()
            audio_id = str(data.get("audioFormatId", "")).strip()
            format_selector = f"{video_id}+{audio_id}" if video_id and audio_id and video_id != audio_id else video_id or audio_id
            args = ["--newline", "--no-warnings", "--merge-output-format", "mp4"]
            if format_selector:
                args.extend(["-f", format_selector])
            args.extend(["-o", output_path, url])
            thread_args = (args,)

        job_id = uuid.uuid4().hex
        job = Job(id=job_id, title=title, status="Starting")
        JobStore.add_job(job_id, job)
        thread = threading.Thread(target=self.download_worker, args=(*thread_args, job_id), daemon=True)
        thread.start()

        self.send_json({"accepted": True, "message": "Download started", "job": vars(job)}, HTTPStatus.OK)

    @staticmethod
    def download_worker(*args: object) -> None:
        """Worker function to handle the download process."""
        job_id = str(args[-1])
        download_args = [str(arg) for arg in args[:-1]]

        try:
            logger.info("Starting download job %s with args: %s", job_id, download_args)
            JobStore.update_status(job_id, "Downloading")
            if len(download_args) == 1:
                run_yt_dlp([download_args[0]], timeout=24 * 60 * 60, progress_callback=lambda percent, speed: JobStore.update_progress(job_id, percent, speed, "Downloading"))
                JobStore.update_status(job_id, "Completed")
                return

            video_url, audio_url, output_path = download_args
            with tempfile.TemporaryDirectory(prefix="echodrop-media-") as directory:
                inputs = []
                if video_url:
                    video_path = str(Path(directory) / "video.%(ext)s")
                    run_yt_dlp(["--newline", "--no-warnings", "-o", video_path, str(video_url)], timeout=24 * 60 * 60, progress_callback=lambda percent, speed: JobStore.update_progress(job_id, percent, speed, "Video"))
                    video_file = next(Path(directory).glob("video.*"))
                    inputs.extend(["-i", str(video_file)])
                if audio_url:
                    audio_path = str(Path(directory) / "audio.%(ext)s")
                    run_yt_dlp(["--newline", "--no-warnings", "-o", audio_path, str(audio_url)], timeout=24 * 60 * 60, progress_callback=lambda percent, speed: JobStore.update_progress(job_id, percent, speed, "Audio"))
                    audio_file = next(Path(directory).glob("audio.*"))
                    inputs.extend(["-i", str(audio_file)])

                destination = str(output_path)
                if video_url:
                    command = ["ffmpeg", "-y", *inputs, "-map", "0:v:0"]
                    if audio_url:
                        command.extend(["-map", "1:a:0"])
                    command.extend(["-c:v", "copy", "-c:a", "aac", destination])
                else:
                    command = ["ffmpeg", "-y", *inputs, "-map", "0:a:0", "-vn", "-c:a", "aac", destination]

                JobStore.update_status(job_id, "Merging")
                completed = run_ffmpeg(
                    command,
                    None,
                    lambda percent, speed: JobStore.update_progress(job_id, percent, speed, "Merging"),
                )
                if completed.returncode:
                    msg = (completed.stderr.strip().splitlines()[-1]
                           if completed.stderr.strip() else "ffmpeg merge failed")
                    raise RuntimeError(msg)  # noqa: TRY301, it's fine to raise here.
            JobStore.update_status(job_id, "Completed")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            logger.exception("Download failed")
            JobStore.update_status(job_id, f"Failed: {error}")


def serve() -> None:
    """Start the HTTP server."""
    ip = Config.data().ip
    port = Config.data().port
    logger.info("Starting EchoDrop server on %s:%d", ip, port)
    ThreadingHTTPServer((ip, port), EchoDropRequestHandler).serve_forever()
