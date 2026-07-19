import os
from pathlib import Path

from creart import it

from src.config import Config
from src.metadata import SongMetadata
from src.models import PlaylistInfo
from src.types import DownloadPathContext
from src.utils import (
    ttml_convent,
    get_song_name_and_dir_path,
    get_suffix,
    get_cover_container_dir,
)


def save(
    song: bytes,
    codec: str,
    metadata: SongMetadata,
    playlist: PlaylistInfo = None,
    path_context: DownloadPathContext = None,
):
    song_name, dir_path = get_song_name_and_dir_path(codec.upper(), metadata, playlist, path_context)
    suffix = get_suffix(codec, it(Config).download.atmosConventToM4a)
    if os.name == "nt":
        full_len = len(str(dir_path / f"{song_name}{suffix}"))
        if full_len > 240:
            overflow = full_len - 230
            trim = max(16, len(song_name) - overflow)
            song_name = song_name[:trim].rstrip(" .") + "…"
    dir_path.mkdir(parents=True, exist_ok=True)
    song_path = dir_path / Path(song_name + suffix)
    song_path.parent.mkdir(parents=True, exist_ok=True)
    with open(song_path, "wb") as f:
        f.write(song)
    if it(Config).download.saveCover and metadata.cover:
        cover_dir = get_cover_container_dir(dir_path, path_context)
        cover_path = cover_dir / Path(f"cover.{it(Config).download.coverFormat}")
        if not cover_path.exists():
            cover_dir.mkdir(parents=True, exist_ok=True)
            with open(cover_path.absolute(), "wb") as f:
                f.write(metadata.cover)
    if it(Config).download.saveLyrics and metadata.lyrics:
        lrc = ttml_convent(metadata.lyrics)
        if lrc:
            if it(Config).download.lyricsFormat == "ttml":
                lrc_path = dir_path / Path(song_name + ".ttml")
            else:
                lrc_path = dir_path / Path(song_name + ".lrc")
            lrc_path.write_text(lrc, encoding="utf-8")
    return song_path.absolute()
