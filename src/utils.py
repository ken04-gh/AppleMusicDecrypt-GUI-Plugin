import asyncio
import concurrent.futures
import json
import os
import subprocess
import time
from asyncio import AbstractEventLoop
from copy import deepcopy
from datetime import datetime, timedelta
from distutils.version import LooseVersion
from itertools import islice
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import m3u8
import regex
from bs4 import BeautifulSoup
from creart import it
from pydantic import ValidationError

from src.config import Config, CONFIG_VERSION
from src.logger import GlobalLogger
from src.models import PlaylistInfo
from src.models.album_meta import Tracks
from src.types import *

executor_pool = concurrent.futures.ThreadPoolExecutor()


def hidden_subprocess_kwargs() -> dict:
    """Prevent console windows for background child processes on Windows."""
    if os.name != "nt":
        return {}
    kwargs: dict = {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    kwargs["startupinfo"] = startupinfo
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if flags:
        kwargs["creationflags"] = flags
    return kwargs
background_tasks = set()


def check_url(url):
    pattern = regex.compile(
        r'^(?:https:\/\/(?:beta\.music|music)\.apple\.com\/(\w{2})(?:\/album|\/album\/.+))\/(?:id)?(\d[^\D]+)(?:$|\?)')
    result = regex.findall(pattern, url)
    return result[0][0], result[0][1]


def check_playlist_url(url):
    pattern = regex.compile(
        r'^(?:https:\/\/(?:beta\.music|music)\.apple\.com\/(\w{2})(?:\/playlist|\/playlist\/.+))\/(?:id)?(pl\.[\w-]+)(?:$|\?)')
    result = regex.findall(pattern, url)
    return result[0][0], result[0][1]


def byte_length(i):
    return (i.bit_length() + 7) // 8


def find_best_codec(parsed_m3u8: m3u8.M3U8, codec: str) -> Optional[m3u8.Playlist]:
    available_medias = [playlist for playlist in parsed_m3u8.playlists
                        if regex.match(CodecRegex.get_pattern_by_codec(codec), playlist.stream_info.audio)]
    available_medias.sort(key=lambda x: x.stream_info.average_bandwidth, reverse=True)
    if codec == Codec.ALAC:
        limited_medias = [media for media in available_medias
                          if int(media.media[0].extras["bit_depth"]) <= it(Config).download.maxBitDepth
                          and int(media.media[0].extras["sample_rate"]) <= it(Config).download.maxSampleRate]
    else:
        limited_medias = available_medias
    if not limited_medias:
        return None
    return limited_medias[0]


def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def timeit(func):
    async def process(func, *args, **params):
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **params)
        else:
            return func(*args, **params)

    async def helper(*args, **params):
        start = time.time()
        result = await process(func, *args, **params)
        it(GlobalLogger).logger.debug(f'{func.__name__}: {time.time() - start}')
        return result

    return helper


def get_digit_from_string(text: str) -> int:
    return int(''.join(filter(str.isdigit, text)))


def ttml_convent(ttml: str) -> str:
    if it(Config).download.lyricsFormat == "ttml":
        return ttml

    b = BeautifulSoup(ttml, features="xml")
    lrc_lines = []

    for item in b.tt.body.children:
        for lyric in item.children:
            h, m, s, ms = 0, 0, 0, 0
            lyric_time: str = lyric.get("begin")
            if not lyric_time:
                return ""
                # raise NotTimeSyncedLyricsException
            if lyric_time.find('.') == -1:
                lyric_time += '.000'
            match lyric_time.count(":"):
                case 0:
                    split_time = lyric_time.split(".")
                    s, ms = get_digit_from_string(split_time[0]), get_digit_from_string(split_time[1])
                case 1:
                    split_time = lyric_time.split(":")
                    s_ms = split_time[-1]
                    del split_time[-1]
                    split_time.extend(s_ms.split("."))
                    m, s, ms = (get_digit_from_string(split_time[0]), get_digit_from_string(split_time[1]),
                                get_digit_from_string(split_time[2]))
                case 2:
                    split_time = lyric_time.split(":")
                    s_ms = split_time[-1]
                    del split_time[-1]
                    split_time.extend(s_ms.split("."))
                    h, m, s, ms = (get_digit_from_string(split_time[0]), get_digit_from_string(split_time[1]),
                                   get_digit_from_string(split_time[2]), get_digit_from_string(split_time[3]))
            lrc_lines.append(
                f"[{str(m + h * 60).rjust(2, '0')}:{str(s).rjust(2, '0')}.{str(int(ms / 10)).rjust(2, '0')}]{lyric.text}")
            if "translation" in it(Config).download.lyricsExtra and b.tt.head.metadata.iTunesMetadata.translation:
                trans_type = b.tt.head.metadata.iTunesMetadata.translation.get("type")
                for translation in b.tt.head.metadata.iTunesMetadata.translation.children:
                    if lyric.get("itunes:key") == translation.get("for"):
                        if trans_type == "replacement":
                            del lrc_lines[-1]
                        lrc_lines.append(
                            f"[{str(m + h * 60).rjust(2, '0')}:{str(s).rjust(2, '0')}.{str(int(ms / 10)).rjust(2, '0')}]{translation.text}")
            if "pronunciation" in it(Config).download.lyricsExtra and b.tt.head.metadata.iTunesMetadata.transliteration:
                for transliteration in b.tt.head.metadata.iTunesMetadata.transliteration.children:
                    if lyric.get("itunes:key") == transliteration.get("for"):
                        lrc_lines.append(
                            f"[{str(m + h * 60).rjust(2, '0')}:{str(s).rjust(2, '0')}.{str(int(ms / 10)).rjust(2, '0')}]{transliteration.text}")
    return "\n".join(lrc_lines)


def get_download_base_dir() -> Path:
    fmt = (it(Config).download.dirPathFormat or "").strip() or "downloads"
    prefix = fmt.split("{", 1)[0].rstrip("/\\") if "{" in fmt else fmt.rstrip("/\\")
    if not prefix:
        prefix = "downloads"
    return Path(prefix)


def get_song_name_format() -> str:
    fmt = (it(Config).download.songNameFormat or "").strip()
    return fmt or "{artist} - {title}"


def check_song_exists(
    metadata,
    codec: str,
    playlist: PlaylistInfo = None,
    path_context: DownloadPathContext = None,
):
    song_name, dir_path = get_song_name_and_dir_path(codec, metadata, playlist, path_context)
    return (Path(dir_path) / Path(song_name + get_suffix(codec, it(Config).download.atmosConventToM4a))).exists()


def get_valid_filename(filename: str):
    return "".join(i for i in filename if i not in ["<", ">", ":", "\"", "/", "\\", "|", "?", "*"])


def get_valid_dir_name(dirname: str):
    return regex.sub(r"\.+$", "", get_valid_filename(dirname))


def get_codec_from_codec_id(codec_id: str) -> str:
    codecs = [Codec.AC3, Codec.EC3, Codec.AAC, Codec.ALAC, Codec.AAC_BINAURAL, Codec.AAC_DOWNMIX]
    for codec in codecs:
        if regex.match(CodecRegex.get_pattern_by_codec(codec), codec_id):
            return codec
    return ""


def get_song_id_from_m3u8(m3u8_url: str) -> str:
    parsed_m3u8 = m3u8.load(m3u8_url)
    return regex.search(r"_A(\d*)_", parsed_m3u8.playlists[0].uri)[1]


def if_raw_atmos(codec: str, convent_atmos: bool):
    if (codec == Codec.EC3 or codec == Codec.AC3) and not convent_atmos:
        return True
    return False


def get_suffix(codec: str, convent_atmos: bool):
    if not convent_atmos and codec == Codec.EC3:
        return ".ec3"
    elif not convent_atmos and codec == Codec.AC3:
        return ".ac3"
    else:
        return ".m4a"


def playlist_metadata_to_params(playlist: PlaylistInfo):
    return {"playlistName": playlist.data[0].attributes.name,
            "playlistCuratorName": playlist.data[0].attributes.curatorName}


def get_audio_info_str(metadata, codec: str):
    if all([bool(metadata.bit_depth), bool(metadata.sample_rate), bool(metadata.sample_rate_kHz)]):
        return it(Config).download.audioInfoFormat.format(bit_depth=metadata.bit_depth,
                                                          sample_rate=metadata.sample_rate,
                                                          sample_rate_kHz=metadata.sample_rate_kHz, codec=codec)
    else:
        return ""


def get_path_safe_dict(param: dict):
    new_param = deepcopy(param)
    for key, val in new_param.items():
        if isinstance(val, str):
            new_param[key] = get_valid_filename(str(val))
    return new_param


def _sanitize_path(path: Path) -> Path:
    is_abs = path.is_absolute()
    sanitized_parts = [
        part if i == 0 and is_abs else get_valid_dir_name(part)
        for i, part in enumerate(path.parts)
    ]
    return Path(*sanitized_parts)


def _song_folder_name(metadata, path_context: DownloadPathContext) -> str:
    safe_title = get_valid_dir_name(metadata.title or "Unknown")
    if path_context.kind in ("album", "library_album") and metadata.tracknum:
        return get_valid_dir_name(f"{metadata.tracknum:02d} {safe_title}")
    if path_context.kind in ("playlist", "library_playlist") and metadata.playlist_index:
        return get_valid_dir_name(f"{metadata.playlist_index:02d} {safe_title}")
    return safe_title


def _resolve_download_dir(root: Path, path_context: DownloadPathContext, metadata) -> Path:
    parent = (
        get_valid_dir_name(path_context.parent_container)
        if path_context.parent_container else None
    )
    container = get_valid_dir_name(path_context.container_name or "Unknown")

    if path_context.kind in ("song", "library_song"):
        if parent:
            return _sanitize_path(root / parent / _song_folder_name(metadata, path_context))
        return _sanitize_path(root / container)

    path = root
    if parent:
        path = path / parent
    path = path / container / _song_folder_name(metadata, path_context)
    return _sanitize_path(path)


def get_cover_container_dir(dir_path: Path, path_context: DownloadPathContext | None) -> Path:
    if not path_context or path_context.kind in ("song", "library_song"):
        return dir_path
    root = get_download_base_dir()
    parent = (
        get_valid_dir_name(path_context.parent_container)
        if path_context.parent_container else None
    )
    container = get_valid_dir_name(path_context.container_name or "Unknown")
    if parent:
        return root / parent / container
    return root / container


def get_song_name_and_dir_path(
    codec: str,
    metadata,
    playlist: PlaylistInfo = None,
    path_context: DownloadPathContext = None,
):
    safe_meta = get_path_safe_dict(metadata.model_dump())

    if path_context:
        dir_path = _resolve_download_dir(get_download_base_dir(), path_context, metadata)
        song_name = get_song_name_format().format(
            codec=codec,
            total_tracks=metadata.track_total[metadata.disk],
            total_disks=metadata.disk_total,
            audio_info=get_audio_info_str(metadata, codec),
            **safe_meta,
        )
    elif playlist:
        safe_pl_meta = get_path_safe_dict(playlist_metadata_to_params(playlist))
        song_name = it(Config).download.playlistSongNameFormat.format(
            codec=codec,
            playlistSongIndex=metadata.playlist_index,
            audio_info=get_audio_info_str(metadata, codec),
            total_tracks=metadata.track_total[metadata.disk],
            total_disks=metadata.disk_total,
            **safe_meta, **safe_pl_meta,
        )
        dir_path = Path(it(Config).download.playlistDirPathFormat.format(codec=codec, **safe_meta, **safe_pl_meta))
    else:
        song_name = get_song_name_format().format(
            codec=codec,
            total_tracks=metadata.track_total[metadata.disk],
            total_disks=metadata.disk_total,
            audio_info=get_audio_info_str(metadata, codec),
            **safe_meta,
        )
        dir_fmt = (it(Config).download.dirPathFormat or "").strip() or "downloads"
        dir_path = Path(dir_fmt.format(codec=codec, **safe_meta))

    song_name = get_valid_filename(song_name)
    if not path_context:
        dir_path = _sanitize_path(dir_path)
    return song_name, dir_path


_SONG_TRACK_TYPES = frozenset({"songs", "library-songs"})


def parse_song_from_apple_url(url: str) -> Optional[tuple[str, str]]:
    """Parse a canonical Apple Music web URL into (song_id, storefront)."""
    from src.url import AppleMusicURL, URLType

    if not url:
        return None
    parsed = AppleMusicURL.parse_url(url)
    if parsed and parsed.type == URLType.Song:
        return parsed.id, parsed.storefront
    return None


def song_id_from_apple_music_url(url: str) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(url)
    query_ids = parse_qs(parsed.query).get("i")
    if query_ids and str(query_ids[0]).isdigit():
        return str(query_ids[0])
    paths = [part for part in parsed.path.split("/") if part]
    if len(paths) >= 3 and paths[1] == "song":
        cand = paths[-1].split("?")[0]
        if cand.isdigit():
            return cand
    return None


def storefront_from_apple_music_url(url: str, fallback: str) -> str:
    if not url:
        return fallback
    paths = [part for part in urlparse(url).path.split("/") if part]
    return paths[0] if paths else fallback


def resolve_catalog_track(track, default_storefront: str) -> Optional[tuple[str, str]]:
    """Resolve catalog adamId and storefront from a playlist/album track entry."""
    track_type = getattr(track, "type", None)
    if track_type and track_type not in _SONG_TRACK_TYPES:
        return None

    storefront = default_storefront
    song_id: Optional[str] = None
    attrs = getattr(track, "attributes", None)

    if attrs:
        track_url = getattr(attrs, "url", None) or ""
        if track_url:
            storefront = storefront_from_apple_music_url(track_url, default_storefront)
            song_id = song_id_from_apple_music_url(track_url)

        play_params = getattr(attrs, "playParams", None)
        if play_params and not song_id:
            catalog_id = getattr(play_params, "catalogId", None)
            if catalog_id and str(catalog_id).isdigit():
                song_id = str(catalog_id)
            elif getattr(play_params, "kind", None) == "song":
                raw = getattr(play_params, "id", None)
                if raw and str(raw).isdigit():
                    song_id = str(raw)

    relationships = getattr(track, "relationships", None)
    if relationships and not song_id:
        catalog = getattr(relationships, "catalog", None)
        if catalog and catalog.data:
            ref_id = catalog.data[0].id
            if ref_id and str(ref_id).isdigit():
                song_id = str(ref_id)

    if not song_id:
        href = getattr(track, "href", None) or ""
        href_match = regex.search(r"/songs/(\d+)", href)
        if href_match:
            song_id = href_match.group(1)

    if not song_id:
        raw_id = getattr(track, "catalog_id", None) or getattr(track, "id", None)
        if raw_id and str(raw_id).isdigit():
            song_id = str(raw_id)

    if not song_id or str(song_id).startswith("i."):
        return None
    return song_id, storefront


def catalog_song_id_from_track(track, default_storefront: str = "us") -> Optional[str]:
    resolved = resolve_catalog_track(track, default_storefront)
    return resolved[0] if resolved else None


def playlist_write_song_index(playlist: PlaylistInfo, default_storefront: str = "us"):
    for track_index, track in enumerate(playlist.data[0].relationships.tracks.data or []):
        catalog_id = getattr(track, "catalog_id", None)
        if catalog_id and str(catalog_id).isdigit() and not str(catalog_id).startswith("i."):
            playlist.songIdIndexMapping[str(catalog_id)] = track_index + 1
            continue
        resolved = resolve_catalog_track(track, default_storefront)
        if resolved:
            playlist.songIdIndexMapping[resolved[0]] = track_index + 1
    return playlist


def convent_mac_timestamp_to_datetime(timestamp: int):
    d = datetime.strptime("01-01-1904", "%m-%d-%Y")
    return d + timedelta(seconds=timestamp)


def check_dep():
    deps = ["ffmpeg", "gpac", "MP4Box", "mp4edit", "mp4extract", "mp4decrypt"]
    if it(Config).localInstance.enable:
        deps.append("qemu-system-x86_64")
    for dep in deps:
        try:
            subprocess.run(
                dep, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **hidden_subprocess_kwargs(),
            )
        except FileNotFoundError:
            return False, dep
    return True, None


async def check_song_existence(adam_id: str, storefront: str) -> bool:
    """Return True if the song exists on its catalog storefront or any account region."""
    from src.api import WebAPI
    from src.grpc.manager import WrapperManager

    try:
        if await it(WebAPI).song_exist(adam_id, storefront):
            return True
    except Exception:
        pass

    try:
        regions = (await it(WrapperManager).status()).regions
    except Exception:
        regions = []

    for m_region in regions:
        if m_region.upper() == storefront.upper():
            continue
        try:
            if await it(WebAPI).song_exist(adam_id, m_region):
                return True
        except Exception:
            pass
    return False


async def check_album_existence(album_id: str, storefront: str) -> bool:
    from src.api import WebAPI
    from src.grpc.manager import WrapperManager

    try:
        if await it(WebAPI).album_exist(album_id, storefront):
            return True
    except Exception:
        pass

    try:
        regions = (await it(WrapperManager).status()).regions
    except Exception:
        regions = []

    for m_region in regions:
        if m_region.upper() == storefront.upper():
            continue
        try:
            if await it(WebAPI).album_exist(album_id, m_region):
                return True
        except Exception:
            pass
    return False


async def run_sync(task: Callable, *args):
    return await it(AbstractEventLoop).run_in_executor(executor_pool, task, *args)


def safely_create_task(coro):
    task = it(AbstractEventLoop).create_task(coro)
    background_tasks.add(task)

    def done_callback(*args):
        background_tasks.remove(task)
        if task.exception():
            try:
                raise task.exception()
            except Exception as e:
                it(GlobalLogger).logger.exception(e)

    task.add_done_callback(done_callback)


def count_total_track_and_disc(tracks: Tracks):
    disc_count = tracks.data[-1].attributes.discNumber
    track_count = {}
    for track in tracks.data:
        if track_count.get(track.attributes.discNumber, 0) < track.attributes.trackNumber:
            track_count[track.attributes.discNumber] = track.attributes.trackNumber
    return disc_count, track_count


def get_tasks_num():
    return len(background_tasks)


def query_language(region: str):
    with open("assets/storefronts.json", "r") as f:
        storefronts = json.load(f)
        for storefront in storefronts["data"]:
            if storefront["id"].upper() == region.upper():
                return storefront["attributes"]["defaultLanguageTag"], storefront["attributes"]["supportedLanguageTags"]
        return None


def language_exist(region: str, language: str):
    result = query_language(region)
    if not result:
        return False
    _, languages = result
    return language in languages


def resolve_api_language(storefront: str, preferred: str = "") -> str:
    """Pick Apple Music API language: user preference, else storefront default."""
    preferred = (preferred or it(Config).region.language or "").strip()
    if preferred and language_exist(storefront, preferred):
        return preferred
    default_lang, _ = query_language(storefront) or (None, None)
    if default_lang:
        return default_lang
    return preferred or "en-US"


def config_outdated():
    return LooseVersion(it(Config).version) < LooseVersion(CONFIG_VERSION)
