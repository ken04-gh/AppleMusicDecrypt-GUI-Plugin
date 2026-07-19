from __future__ import annotations

import hashlib
import plistlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from src.types import Codec, M3U8Info, prefetchKey
from src.utils import get_codec_from_codec_id


IMPORT_READY = "可导入"
IMPORT_NEEDS_CONFIRM = "需确认"
IMPORT_UNSUPPORTED = "当前版本暂不支持"
IMPORT_INCOMPLETE = "资源不完整"
IMPORT_NOT_IMPORTABLE = "不可导入"


@dataclass
class CacheSegment:
    seq: int
    offset: int
    length: int
    path: Path


@dataclass
class CacheStream:
    stream_id: str = ""
    stream_dir: Path = Path()
    network_url: str = ""
    local_playlist: Optional[Path] = None
    complete: bool = False
    peak_bandwidth: int = 0
    unique_id: str = ""
    codec_id: str = ""
    codec: str = ""
    keys: list[str] = field(default_factory=list)
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    init_segments: list[CacheSegment] = field(default_factory=list)
    media_segments: list[CacheSegment] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        return sum(seg.length for seg in self.init_segments + self.media_segments)

    @property
    def integrity_status(self) -> str:
        if not self.complete:
            return "未完整缓存"
        if self.errors:
            return "结构异常"
        if not self.init_segments or not self.media_segments:
            return "资源不完整"
        return "完整"


@dataclass
class CacheImportCandidate:
    candidate_id: str
    source_root: Path
    media_root: Path
    package_path: Path
    package_name: str
    track_title: str
    track_artist: str
    track_album: str
    resource_type: str
    integrity_status: str
    import_status: str
    note: str = ""
    adam_id: str = ""
    asset_id: str = ""
    codec: str = ""
    codec_id: str = ""
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    stream_id: str = ""
    stream_path: str = ""
    stream_count: int = 0
    complete_stream_count: int = 0
    size_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "source_root": str(self.source_root),
            "media_root": str(self.media_root),
            "package_path": str(self.package_path),
            "package_name": self.package_name,
            "track_title": self.track_title,
            "track_artist": self.track_artist,
            "track_album": self.track_album,
            "resource_type": self.resource_type,
            "integrity_status": self.integrity_status,
            "import_status": self.import_status,
            "note": self.note,
            "adam_id": self.adam_id,
            "asset_id": self.asset_id,
            "codec": self.codec,
            "codec_id": self.codec_id,
            "bit_depth": self.bit_depth,
            "sample_rate": self.sample_rate,
            "stream_id": self.stream_id,
            "stream_path": self.stream_path,
            "stream_count": self.stream_count,
            "complete_stream_count": self.complete_stream_count,
            "size_bytes": self.size_bytes,
        }


def scan_apple_music_cache(
    root: str | Path,
    on_progress: Optional[Callable[[int, int, Path], None]] = None,
) -> list[CacheImportCandidate]:
    source_root, media_root = resolve_cache_roots(Path(root))
    packages = sorted(media_root.rglob("*.movpkg"))
    play_queue = _load_play_queue_metadata(source_root)
    candidates: list[CacheImportCandidate] = []
    for idx, package_path in enumerate(packages, 1):
        if on_progress:
            on_progress(idx, len(packages), package_path)
        candidates.append(parse_movpkg(package_path, source_root, media_root, play_queue))
    return candidates


def resolve_cache_roots(root: Path) -> tuple[Path, Path]:
    source_root = root.expanduser().resolve()
    if not source_root.exists():
        raise FileNotFoundError(f"缓存目录不存在: {source_root}")
    if source_root.name.lower() == "media":
        return source_root.parent, source_root
    media_root = source_root / "Media"
    if media_root.is_dir():
        return source_root, media_root
    if any(source_root.rglob("*.movpkg")):
        return source_root, source_root
    raise FileNotFoundError(f"未找到 Media 或 movpkg 缓存包: {source_root}")


def parse_movpkg(
    package_path: Path,
    source_root: Path,
    media_root: Path,
    play_queue: Optional[dict[str, dict[str, str]]] = None,
) -> CacheImportCandidate:
    package_path = package_path.resolve()
    source_root = source_root.resolve()
    media_root = media_root.resolve()
    master_map = _parse_master_codec_map(package_path)
    boot_text = _read_text(package_path / "boot.xml")
    playlist_texts = [_read_text(m3u8) for m3u8 in package_path.rglob("*.m3u8")]
    streams = _parse_streams(package_path, master_map)
    selected = _select_stream(streams)
    adam_id = _extract_adam_id(
        selected.network_url if selected else "",
        boot_text,
        *playlist_texts,
    )
    asset_id = _extract_asset_id(
        selected.network_url if selected else "",
        boot_text,
        *playlist_texts,
    )
    title, artist, album = _path_metadata(package_path, media_root)
    if play_queue:
        meta = play_queue.get(adam_id) if adam_id else None
        if not meta and not adam_id:
            matched_id, meta = _match_play_queue_metadata(title, artist, album, play_queue)
            if matched_id:
                adam_id = matched_id
        if meta:
            title = meta.get("title") or title
            artist = meta.get("artist") or artist
            album = meta.get("album") or album

    stream_count = len(streams)
    complete_count = sum(1 for stream in streams if stream.complete)
    if not stream_count:
        integrity_status = "无可识别流"
        import_status = IMPORT_NOT_IMPORTABLE
        note = "未找到可解析的 StreamInfoBoot.xml"
    elif not selected:
        integrity_status = "资源不完整"
        import_status = IMPORT_INCOMPLETE if complete_count else IMPORT_NEEDS_CONFIRM
        note = "没有完整且受支持的本地音频流"
    else:
        integrity_status = selected.integrity_status
        note_parts = list(selected.errors)
        if not adam_id:
            import_status = IMPORT_NEEDS_CONFIRM
            note_parts.append("无法从 m3u8/NetworkURL 稳定识别曲目 ID")
        elif selected.errors:
            import_status = IMPORT_INCOMPLETE
        elif not selected.codec:
            import_status = IMPORT_UNSUPPORTED
            note_parts.append("无法识别受支持的音频编码")
        elif not title or not artist or not album:
            import_status = IMPORT_NEEDS_CONFIRM
            note_parts.append("缺少最小曲目信息")
        else:
            import_status = IMPORT_READY
        note = "；".join(dict.fromkeys(part for part in note_parts if part))

    candidate_id = _candidate_id(package_path, selected.stream_dir if selected else package_path)
    return CacheImportCandidate(
        candidate_id=candidate_id,
        source_root=source_root,
        media_root=media_root,
        package_path=package_path,
        package_name=package_path.name,
        track_title=title or package_path.stem,
        track_artist=artist or "未知艺人",
        track_album=album or "未知专辑",
        resource_type="Apple Music movpkg",
        integrity_status=integrity_status,
        import_status=import_status,
        note=note,
        adam_id=adam_id,
        asset_id=asset_id,
        codec=selected.codec if selected else "",
        codec_id=selected.codec_id if selected else "",
        bit_depth=selected.bit_depth if selected else None,
        sample_rate=selected.sample_rate if selected else None,
        stream_id=selected.stream_id if selected else "",
        stream_path=str(selected.stream_dir) if selected else "",
        stream_count=stream_count,
        complete_stream_count=complete_count,
        size_bytes=selected.size_bytes if selected else 0,
    )


def build_cached_staged_payload(candidate: CacheImportCandidate) -> tuple[bytes, M3U8Info]:
    stream_dir = Path(candidate.stream_path)
    if not stream_dir.is_dir():
        raise FileNotFoundError(f"缓存流目录不存在: {stream_dir}")
    master_map = _parse_master_codec_map(candidate.package_path)
    stream = _parse_stream_info(stream_dir, master_map)
    if stream.errors:
        raise ValueError("缓存资源不完整: " + "；".join(stream.errors))
    if not stream.init_segments or not stream.media_segments:
        raise ValueError("缓存资源不完整: 缺少 initfrag 或 frag")
    expected_offset = 0
    chunks: list[bytes] = []
    for segment in sorted(stream.init_segments + stream.media_segments, key=lambda item: item.offset):
        if segment.offset != expected_offset:
            raise ValueError(f"缓存分片偏移不连续: 期望 {expected_offset}, 实际 {segment.offset}")
        data = segment.path.read_bytes()
        if len(data) != segment.length:
            raise ValueError(f"缓存分片长度异常: {segment.path.name}")
        chunks.append(data)
        expected_offset += len(data)
    if not stream.codec_id:
        raise ValueError("无法识别缓存音频编码")
    return b"".join(chunks), M3U8Info(
        uri=f"local-cache://{candidate.candidate_id}",
        keys=stream.keys,
        codec_id=stream.codec_id,
        bit_depth=stream.bit_depth,
        sample_rate=stream.sample_rate,
    )


def _parse_streams(package_path: Path, master_map: dict[str, str]) -> list[CacheStream]:
    streams: list[CacheStream] = []
    seen_dirs: set[Path] = set()
    for stream_boot in sorted(package_path.rglob("StreamInfoBoot.xml")):
        if stream_boot.parent in seen_dirs:
            continue
        seen_dirs.add(stream_boot.parent)
        streams.append(_parse_stream_info(stream_boot.parent, master_map))
    return streams


def _parse_stream_info(stream_dir: Path, master_map: dict[str, str]) -> CacheStream:
    stream = CacheStream(stream_id=stream_dir.name, stream_dir=stream_dir)
    root = _parse_xml(stream_dir / "StreamInfoBoot.xml")
    if root is None:
        stream.errors.append("StreamInfoBoot.xml 读取失败")
        return stream

    stream.complete = _find_text(root, "Complete").upper() == "YES"
    stream.peak_bandwidth = _safe_int(_find_text(root, "PeakBandwidth"))
    stream.network_url = _find_text(root, "NetworkURL")
    stream.unique_id = _find_text(root, "UniqueIdentifier")
    local_playlist = _find_text(root, "PathToLocalCopy")
    if local_playlist:
        stream.local_playlist = _resolve_cache_child(stream_dir, local_playlist)

    stream.codec_id = _codec_id_for_stream(stream.network_url, stream.unique_id, master_map)
    stream.codec = get_codec_from_codec_id(stream.codec_id) if stream.codec_id else ""
    stream.sample_rate, stream.bit_depth = _audio_info_from_codec_id(stream.codec_id)

    playlist_text = _read_text(stream.local_playlist) if stream.local_playlist else ""
    stream.keys = _extract_keys(playlist_text, stream.codec)

    for node in _findall(root, "ISEG"):
        stream.init_segments.append(_segment_from_node(stream_dir, node))
    for node in _findall(root, "SEG"):
        stream.media_segments.append(_segment_from_node(stream_dir, node))

    _validate_stream(stream)
    return stream


def _segment_from_node(stream_dir: Path, node: ET.Element) -> CacheSegment:
    return CacheSegment(
        seq=_safe_int(node.attrib.get("SeqNum", "0")),
        offset=_safe_int(node.attrib.get("Off", "0")),
        length=_safe_int(node.attrib.get("Len", "0")),
        path=_resolve_cache_child(stream_dir, node.attrib.get("PATH") or ""),
    )


def _validate_stream(stream: CacheStream):
    if not stream.complete:
        return
    for segment in stream.init_segments + stream.media_segments:
        if not segment.path.is_file():
            stream.errors.append(f"缺少分片 {segment.path.name}")
            continue
        size = segment.path.stat().st_size
        if segment.length and size != segment.length:
            stream.errors.append(f"分片长度不匹配 {segment.path.name}")
    if stream.local_playlist and not stream.local_playlist.is_file():
        stream.errors.append("缺少本地 m3u8")
    if not stream.keys or len(stream.keys) < 2:
        stream.errors.append("缺少可用 skd key")
    if not stream.codec:
        stream.errors.append("编码不受支持")


def _select_stream(streams: list[CacheStream]) -> Optional[CacheStream]:
    ready = [
        stream for stream in streams
        if stream.complete and not stream.errors and stream.codec
    ]
    if not ready:
        return None
    priority = {
        Codec.ALAC: 0,
        Codec.EC3: 1,
        Codec.AC3: 2,
        Codec.AAC: 3,
        Codec.AAC_BINAURAL: 4,
        Codec.AAC_DOWNMIX: 5,
    }
    return sorted(
        ready,
        key=lambda stream: (priority.get(stream.codec, 99), -stream.peak_bandwidth),
    )[0]


def _parse_master_codec_map(package_path: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    data_dir = package_path / "Data"
    masters = data_dir.rglob("*.m3u8") if data_dir.is_dir() else package_path.rglob("*.m3u8")
    for master in sorted(masters):
        lines = _read_text(master).replace("\x00", "").splitlines()
        pending_audio = ""
        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF"):
                pending_audio = _attr(line, "AUDIO")
                continue
            if pending_audio and line and not line.startswith("#"):
                basename = PurePosixPath(urlparse(line).path).name
                if basename:
                    mapping[basename] = pending_audio
                pending_audio = ""
    return mapping


def _codec_id_for_stream(network_url: str, unique_id: str, master_map: dict[str, str]) -> str:
    basename = PurePosixPath(urlparse(network_url).path).name
    if basename in master_map:
        return master_map[basename]
    lower = network_url.lower()
    if "alac" in lower or re.search(r"_gr2304(?:_|\\.m3u8)", lower):
        return "audio-alac-stereo-48000-24"
    if "binaural" in lower:
        return "audio-stereo-256-binaural"
    if "downmix" in lower:
        return "audio-stereo-256-downmix"
    if "ac3" in lower:
        return "audio-ac3-384"
    if "ec3" in lower or "atmos" in lower:
        return "audio-ec3-7680"
    match = re.search(r"_gr(\d{3})", lower)
    if match:
        return f"audio-stereo-{match.group(1)}"
    for mapped in master_map.values():
        if unique_id and unique_id in mapped:
            return mapped
    return ""


def _audio_info_from_codec_id(codec_id: str) -> tuple[Optional[int], Optional[int]]:
    match = re.match(r"audio-alac-stereo-(\d+)-(\d+)$", codec_id or "")
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _extract_keys(playlist_text: str, codec: str) -> list[str]:
    key_uris = []
    for match in re.finditer(r"#EXT-X-KEY:[^\n]*URI=(?:\"([^\"]+)\"|'([^']+)'|([^,\s]+))", playlist_text or ""):
        key_uris.append(next((group for group in match.groups() if group), ""))
    keys = [prefetchKey]
    suffix = {
        Codec.ALAC: "c23",
        Codec.EC3: "c24",
        Codec.AC3: "c24",
        Codec.AAC: "c22",
        Codec.AAC_BINAURAL: "c24",
        Codec.AAC_DOWNMIX: "c24",
    }.get(codec, "")
    for key in key_uris:
        normalized = key.strip()
        if not normalized or normalized in keys:
            continue
        if normalized.endswith("c6") or (suffix and normalized.endswith(suffix)):
            keys.append(normalized)
    return keys


def _extract_adam_id(*texts: str) -> str:
    joined = "\n".join(texts)
    for pattern in (
        r"_A(\d{5,})_",
        r"/A(\d{5,})_",
        r"\bA(\d{5,})\b",
        r"(?i)\b(?:adam[-_ ]?id|storeAdamID|songAdamId|storeAdamId)\b[^0-9]{0,24}(\d{5,})",
    ):
        match = re.search(pattern, joined)
        if match:
            return match.group(1)
    return ""


def _extract_asset_id(*texts: str) -> str:
    joined = "\n".join(texts)
    for pattern in (r"\bP(\d{5,})_", r"/p(\d{5,})/", r"/P(\d{5,})_", r"\bP(\d{5,})\b"):
        match = re.search(pattern, joined, flags=re.IGNORECASE)
        if match and set(match.group(1)) != {"0"}:
            return match.group(1)
    return ""


def _path_metadata(package_path: Path, media_root: Path) -> tuple[str, str, str]:
    try:
        parts = package_path.relative_to(media_root).parts
    except ValueError:
        parts = package_path.parts
    title = re.sub(r"^\d+\s+[-. ]*", "", package_path.stem).strip()
    artist = ""
    album = ""
    if len(parts) >= 3:
        artist = parts[-3]
        album = parts[-2]
    elif len(parts) >= 2:
        artist = parts[-2]
    return title, artist, album


def _load_play_queue_metadata(source_root: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for plist_path in source_root.rglob("PlayQueueState.plist"):
        try:
            payload = plistlib.loads(plist_path.read_bytes())
        except Exception:
            continue
        for item in _walk_plist_dicts(payload):
            identifiers = _first_dict(
                item.get("identifiers"),
                (item.get("desc") or {}).get("identifiers") if isinstance(item.get("desc"), dict) else None,
                (item.get("metadata") or {}).get("identifiers") if isinstance(item.get("metadata"), dict) else None,
            )
            adam_id = str(
                identifiers.get("storeAdamID")
                or identifiers.get("storeAdamId")
                or identifiers.get("adamID")
                or identifiers.get("adamId")
                or item.get("storeAdamID")
                or item.get("storeAdamId")
                or item.get("adamID")
                or item.get("adamId")
                or "",
            )
            if not adam_id.isdigit():
                continue
            title = str(
                item.get("line1")
                or item.get("title")
                or item.get("songName")
                or "",
            )
            artist = str(item.get("artistName") or item.get("artist") or "")
            album = str(item.get("albumName") or item.get("album") or "")
            line2_artist, line2_album = _split_line2(str(item.get("line2") or ""))
            result.setdefault(
                adam_id,
                {
                    "title": title,
                    "artist": artist or line2_artist,
                    "album": album or line2_album,
                },
            )
    return result


def _split_line2(line2: str) -> tuple[str, str]:
    separators = (" — ", " - ", " – ")
    sep = next((item for item in separators if item in line2), "")
    if not sep:
        return "", line2
    artist, album = line2.split(sep, 1)
    return artist.strip(), album.strip()


def _match_play_queue_metadata(
    title: str,
    artist: str,
    album: str,
    play_queue: dict[str, dict[str, str]],
) -> tuple[str, Optional[dict[str, str]]]:
    title_key = _normalize_match_text(title)
    artist_key = _normalize_match_text(artist)
    album_key = _normalize_match_text(album)
    if not title_key or not artist_key:
        return "", None
    for adam_id, meta in play_queue.items():
        if _normalize_match_text(meta.get("title", "")) != title_key:
            continue
        if _normalize_match_text(meta.get("artist", "")) != artist_key:
            continue
        meta_album = _normalize_match_text(meta.get("album", ""))
        if album_key and meta_album and meta_album != album_key:
            continue
        return adam_id, meta
    return "", None


def _normalize_match_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def _walk_plist_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_plist_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_plist_dicts(child)


def _first_dict(*values: Any) -> dict:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _resolve_cache_child(root: Path, raw_path: str) -> Path:
    cleaned = (raw_path or "").strip().replace("\\", "/")
    if not cleaned:
        return root
    parsed = urlparse(cleaned)
    if parsed.scheme == "file":
        cleaned = parsed.path
    candidate = Path(cleaned)
    if candidate.is_absolute():
        return candidate
    return root / cleaned.lstrip("/")


def _candidate_id(package_path: Path, stream_dir: Path) -> str:
    raw = f"{package_path.resolve()}|{stream_dir.resolve()}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").replace("\x00", "")
    except Exception:
        return ""


def _parse_xml(path: Path) -> Optional[ET.Element]:
    try:
        return ET.fromstring(_read_text(path))
    except Exception:
        return None


def _local_name(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _find_text(root: ET.Element, name: str) -> str:
    for node in root.iter():
        if _local_name(node) == name:
            return (node.text or "").strip()
    return ""


def _findall(root: ET.Element, name: str) -> list[ET.Element]:
    return [node for node in root.iter() if _local_name(node) == name]


def _attr(line: str, name: str) -> str:
    match = re.search(rf'{re.escape(name)}="([^"]*)"', line)
    return match.group(1) if match else ""


def _safe_int(value: str | int | None) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0
