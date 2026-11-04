from pathlib import Path

from yt_dlp.cookies import YoutubeDLCookieJar
from yt_dlp.cookies import load_cookies as yt_dlp_load_cookies

from src.echodrop.config import Config


def load_cookies() -> YoutubeDLCookieJar:
    """Load cookies using Config settings."""
    cookies_path = Path(Config.data().cookies_file).resolve()
    if cookies_path.is_file():
        return yt_dlp_load_cookies(str(cookies_path), None, YoutubeDLCookieJar())

    return yt_dlp_load_cookies(None, (Config.data().browser, "Default", None, None), YoutubeDLCookieJar())



def yt_dlp_cookie_args() -> list[str]:
    """Return command-line arguments for yt-dlp to use cookies."""
    cookies_path = Path(Config.data().cookies_file).resolve()
    if cookies_path.is_file():
        return ["--cookies", str(cookies_path)]

    return ["--cookies-from-browser", Config.data().browser, "--cookies-from-browser-profile", "Default"]

