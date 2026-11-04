import json
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import timedelta

from src.echodrop.config import Config
from src.echodrop.cookies import load_cookies


@dataclass
class LessonData(dict):
    """Data class to hold lecture capture metadata."""

    title: str
    duration: int | None
    thumbnail: str | None
    formats: list[dict]

    def __post_init__(self) -> None:
        self.update(asdict(self))


@dataclass
class MediaFormat(dict):
    """Data class to hold media format information."""

    format_id: str
    type: str
    filename: str
    thumbnail: str | None
    quality: str
    size: int | None
    url: str

    def __post_init__(self) -> None:
        self.update(asdict(self))



@dataclass
class LectureData(dict):
    """Data class to hold section metadata."""

    id: str
    url: str
    lesson_url: str
    course: str
    title: str
    meta: str
    thumbnail: str | None
    sources: list[dict]

    def __post_init__(self) -> None:
        self.update(asdict(self))


def parse_duration_seconds(value: str | None) -> int | None:
    """Parse a duration string in ISO 8601 format and return the value in seconds."""
    if not value or not value.startswith("PT"):
        return None
    hours = minutes = seconds = 0.0
    pattern = r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?"
    match = re.fullmatch(pattern, value)
    if not match:
        return None
    hours, minutes, seconds = (float(part or 0) for part in match.groups())
    return int(timedelta(hours=hours, minutes=minutes, seconds=seconds).total_seconds())


def parse_media_url(value: str) -> str:
    """Normalize a media URL by removing query parameters and fragments."""
    parsed = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def parse_media_filename(value: str) -> str:
    """Extract the filename from a media URL, excluding the extension."""
    name = urllib.parse.urlsplit(value).path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def parse_media_thumbnail(media: dict, fallback: str | None) -> str | None:
    """Extract the thumbnail URL from a media dictionary, with a fallback option."""
    thumbnail = media.get("thumbnail") or media.get("thumbnailUri") or media.get("poster")
    if isinstance(thumbnail, dict):
        thumbnail = thumbnail.get("uri")
    return thumbnail or fallback


def parse_derived_thumbnail(value: str, fallback: str | None) -> str | None:
    """Derive a thumbnail URL from a media URL, with a fallback option."""
    match = re.search(r"/(s(\d+)_(?:v|av))\.m3u8$", urllib.parse.urlsplit(value).path)
    if not match:
        return fallback
    parsed = urllib.parse.urlsplit(value)
    thumbnail_path = parsed.path.rsplit("/", 1)[0] + f"/thumbnails{match.group(2)}/2160.jpg"
    url = ("https", "thumbnails.echo360.org.uk", thumbnail_path, "", "")
    return urllib.parse.urlunsplit(url)


def resolve_lesson(url: str) -> LessonData:
    """Resolve a lecture URL to extract metadata."""
    cookies = load_cookies()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(request, timeout=Config.data().request_timeout) as response:
        html = response.read().decode("utf-8")

    match = re.search(r'\["echoPlayerV2FullApp"\]\("((?:\\.|[^"])*)"\)', html)
    if not match:
        msg = "Could not find lecture metadata in the HTML."
        raise ValueError(msg)

    match = re.search(r'\["echoPlayerV2FullApp"\]\("((?:\\.|[^"])*)"\)', html)
    if not match:
        msg = "The authenticated Echo360 lesson player data was not found."
        raise RuntimeError(msg)

    player = json.loads(json.loads(f'"{match.group(1)}"'))
    video = player.get("video") or {}
    posters = video.get("posterMedia") or []
    thumbnail = posters[0].get("uri") if posters else None
    formats = []
    seen_urls = set()
    for media in video.get("playableMedias") or []:
        media_url = parse_media_url(media.get("uri"))
        if not media_url or media_url in seen_urls:
            continue
        filename = parse_media_filename(media_url)
        filename_match = re.fullmatch(r"s\d+_(v|a|av)\.m3u8", urllib.parse.urlsplit(media_url).path.rsplit("/", 1)[-1])
        if not filename_match:
            continue
        suffix = filename_match.group(1)
        media_type = {"v": "Video", "a": "Audio", "av": "Video + audio"}[suffix]
        seen_urls.add(media_url)
        formats.append(MediaFormat(
            format_id=f"echo-{media_type.lower()}-{len(formats)}",
            type=media_type,
            filename=filename,
            thumbnail=parse_media_thumbnail(media, parse_derived_thumbnail(media_url, thumbnail)),
            quality=", ".join(str(item) for item in (media.get("quality") or [])) or "HLS",
            size=None,
            url=media_url,
        ))

    return LessonData(
        title=video.get("title") or "Untitled Lecture",
        duration=parse_duration_seconds(video.get("duration")),
        thumbnail=thumbnail,
        formats=formats,
    )


def resolve_section(url: str) -> list[LectureData]:
    """Resolve a section URL to extract metadata and lecture entries."""
    cookies = load_cookies()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))

    match = re.search(r"/section/([^/]+)", urllib.parse.urlparse(url).path)
    if not match:
        msg = "Could not extract section ID from the URL."
        raise ValueError(msg)
    section_id = match.group(1)

    with opener.open(urllib.request.Request(f"https://echo360.org.uk/section/{section_id}/syllabus", headers={"User-Agent": "Mozilla/5.0"}), timeout=Config.data().request_timeout) as response:
        syllabus = json.loads(response.read().decode("utf-8"))
    lesson_entries = []
    for item in syllabus.get("data", []):
        lesson_info = item.get("lesson", {})
        lesson_data = lesson_info.get("lesson", {})
        available_video = any(
            media.get("mediaType") == "Video" and media.get("isAvailable") is True
            for media in lesson_info.get("medias", [])
        )
        if lesson_data.get("id") and available_video:
            lesson_entries.append(lesson_data)

    lectures = []
    for lesson_data in lesson_entries:
        title = lesson_data.get("title") or lesson_data.get("name") or f"Lecture {len(lectures) + 1}"
        lesson_url = f"https://echo360.org.uk/lesson/{lesson_data['id']}/classroom?focus=Video"
        lectures.append(LectureData(
            id=f"section-{len(lectures)}",
            url=lesson_url,
            lesson_url=lesson_url,
            course="ECHO360 SECTION",
            title=title,
            meta="Click to load streams",
            thumbnail=None,
            sources=[]),
        )
    return lectures

