from urllib.parse import urlparse, parse_qs

import regex
from pydantic import BaseModel


class URLType:
    Song = "song"
    Album = "album"
    Playlist = "playlist"
    LibraryPlaylist = "library_playlist"
    LibraryAlbum = "library_album"
    LibrarySong = "library_song"
    Artist = "artist"


class AppleMusicURL(BaseModel):
    url: str
    storefront: str
    type: str
    id: str

    @classmethod
    def parse_url(cls, url: str):
        parsed_url = urlparse(url)
        paths = [p for p in parsed_url.path.split("/") if p]
        if len(paths) < 3:
            return None
        storefront = paths[0]
        url_type = paths[1]
        if url_type == "library" and len(paths) >= 3:
            resource = paths[2]
            tail_id = paths[3].split("?")[0] if len(paths) >= 4 else ""
            if resource in ("playlist", "playlists") and tail_id.startswith("p."):
                return LibraryPlaylist(url=url, storefront=storefront, id=tail_id, type=URLType.LibraryPlaylist)
            if resource.startswith("p."):
                return LibraryPlaylist(url=url, storefront=storefront, id=resource, type=URLType.LibraryPlaylist)
            if resource in ("album", "albums") and tail_id.startswith("l."):
                return LibraryAlbum(url=url, storefront=storefront, id=tail_id, type=URLType.LibraryAlbum)
            if resource.startswith("l."):
                return LibraryAlbum(url=url, storefront=storefront, id=resource, type=URLType.LibraryAlbum)
            if resource in ("song", "songs") and tail_id.startswith(("i.", "l.")):
                return LibrarySong(url=url, storefront=storefront, id=tail_id, type=URLType.LibrarySong)
            if resource.startswith("i."):
                return LibrarySong(url=url, storefront=storefront, id=resource, type=URLType.LibrarySong)
        if not regex.match(
            r"https://music.apple.com/(.{2})/(song|album|playlist|artist).*/(pl.*|\d*)", url,
        ):
            return None
        url_type = paths[1]
        match url_type:
            case URLType.Song:
                url_id = paths[-1]
                return Song(url=url, storefront=storefront, id=url_id, type=URLType.Song)
            case URLType.Album:
                if not parsed_url.query:
                    url_id = paths[-1]
                    return Album(url=url, storefront=storefront, id=url_id, type=URLType.Album)
                else:
                    url_query = parse_qs(parsed_url.query)
                    if url_query.get("i"):
                        url_id = url_query.get("i")[0]
                        return Song(url=url, storefront=storefront, id=url_id, type=URLType.Song)
                    else:
                        url_id = paths[-1]
                        return Album(url=url, storefront=storefront, id=url_id, type=URLType.Album)
            case URLType.Artist:
                url_id = paths[-1]
                return Artist(url=url, storefront=storefront, id=url_id, type=URLType.Artist)
            case URLType.Playlist:
                url_id = paths[-1]
                return Playlist(url=url, storefront=storefront, id=url_id, type=URLType.Playlist)
        return None


class Song(AppleMusicURL):
    ...


class Album(AppleMusicURL):
    ...


class Playlist(AppleMusicURL):
    ...


class LibraryPlaylist(AppleMusicURL):
    ...


class LibraryAlbum(AppleMusicURL):
    ...


class LibrarySong(AppleMusicURL):
    ...


class Artist(AppleMusicURL):
    ...
