import asyncio
from io import BytesIO
from ssl import SSLError
from typing import Optional, Type

import httpx
import regex
from creart import AbstractCreator, CreateTargetInfo, exists_module, it
from httpx import Request, Response, AsyncHTTPTransport, AsyncClient
from tenacity import retry, retry_if_exception_type, wait_random_exponential, stop_after_attempt, before_sleep_log

from src.config import Config
from src.logger import GlobalLogger
from src.measurer import Measurer
from src.models import *


class NameSolver:
    def get(self, name: str) -> str:
        if name == "aod.itunes.apple.com":
            return it(Config).download.appleCDNIP
        return ''

    def resolve(self, request: Request) -> Request:
        host = request.url.host
        ip = self.get(host)

        if ip:
            request.extensions["sni_hostname"] = host
            request.url = request.url.copy_with(host=ip)

        return request


class AsyncCustomHost(AsyncHTTPTransport):
    def __init__(self, solver: NameSolver, *args, **kwargs) -> None:
        self.solver = solver
        super().__init__(*args, **kwargs)

    async def handle_async_request(self, request: Request) -> Response:
        request = self.solver.resolve(request)
        return await super().handle_async_request(request)


def format_apple_network_error(exc: BaseException) -> str:
    """User-facing Chinese explanation for Apple Music connectivity failures."""
    # Prefer already-localized RuntimeError from _set_token
    existing = str(getattr(exc, "msg", None) or exc)
    if "music.apple.com" in existing and ("代理" in existing or "TLS" in existing or "无法" in existing):
        return existing

    msg = existing
    lower = msg.lower()
    # tenacity wraps the real error
    cause = getattr(exc, "last_attempt", None)
    if cause is not None:
        try:
            inner = cause.exception()
            if inner is not None:
                msg = str(inner)
                lower = msg.lower()
                exc = inner
        except Exception:
            pass
    root = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if root is not None:
        msg = f"{msg} | {root}"
        lower = msg.lower()

    if any(k in lower for k in ("ssl", "unexpected_eof", "eof occurred", "certificate", "wrong version number")):
        return (
            "无法与 music.apple.com 建立 TLS 安全连接（连接在握手阶段被中断）。\n"
            "本机网络多半无法直连 Apple Music（常见于防火墙/运营商拦截）。\n\n"
            "请按顺序尝试：\n"
            "1. 开启可访问 Apple 的代理或 VPN；\n"
            "2. 在「设置」填写 HTTP/HTTPS 代理，例如 http://127.0.0.1:7890；\n"
            "3. 保存设置后点击「重试连接 Apple API」。\n\n"
            f"技术细节: {msg}"
        )
    if any(k in lower for k in ("connecterror", "connection refused", "connection reset",
                                  "name or service not known", "getaddrinfo", "timed out", "timeout",
                                  "network is unreachable", "no route to host")):
        return (
            "无法连接 Apple Music 网站（music.apple.com）。\n"
            "启动时需要访问该站点以获取开发者令牌，与 Apple ID 登录无关。\n\n"
            "请检查：网络是否正常、是否需要代理/VPN、代理地址是否正确。\n"
            "在「设置」配置代理后可重试，无需因此判定为解密内核启动失败。\n\n"
            f"技术细节: {msg}"
        )
    return f"初始化 Apple Music API 失败: {msg}"


class WebAPI:
    client: httpx.AsyncClient
    download_lock: asyncio.Semaphore
    request_lock: asyncio.Semaphore
    token: str
    _music_user_token: str
    _proxy: str
    _token_ready: bool

    def __init__(self, proxy: str, parallel_num: int):
        # Token fetch is deferred to init()/ensure_token() so constructor never
        # hard-crashes the process on network failure.
        self._music_user_token = ""
        self._proxy = (proxy or "").strip()
        self.token = ""
        self._token_ready = False
        self.download_lock = asyncio.Semaphore(parallel_num)
        self.request_lock = asyncio.Semaphore(256)
        self.client = self._make_client()

    def _make_client(self) -> AsyncClient:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Origin": "https://music.apple.com",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        timeout = httpx.Timeout(30.0, connect=15.0, read=60.0, pool=20.0)
        return AsyncClient(
            headers=headers,
            proxy=self._proxy or None,
            timeout=timeout,
            trust_env=True,
        )

    def _rebuild_client(self):
        old = getattr(self, "client", None)
        self.client = self._make_client()
        if old is not None:
            try:
                # AsyncClient.aclose must be awaited; best-effort sync close if available
                if hasattr(old, "_transport"):
                    pass
            except Exception:
                pass

    def set_proxy(self, proxy: str):
        self._proxy = (proxy or "").strip()
        self._rebuild_client()

    def _token_cache_path(self):
        from pathlib import Path
        # Prefer project data/ next to config when available
        try:
            base = Path(it(Config).__dict__.get("_config_path", "")).resolve().parent
            if not base or str(base) == ".":
                base = Path.cwd()
        except Exception:
            base = Path.cwd()
        path = base / "data" / "developer_token.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_token_cache(self) -> bool:
        try:
            path = self._token_cache_path()
            if not path.is_file():
                return False
            token = path.read_text(encoding="utf-8").strip()
            if token.startswith("eyJ") and token.count(".") >= 2:
                self.token = token
                return True
        except Exception:
            pass
        return False

    def _save_token_cache(self):
        try:
            if self.token:
                self._token_cache_path().write_text(self.token, encoding="utf-8")
        except Exception:
            pass

    def _fetch_token_once(self):
        timeout = httpx.Timeout(20.0, connect=12.0, read=30.0)
        with httpx.Client(
            proxy=self._proxy or None,
            timeout=timeout,
            trust_env=True,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                ),
            },
        ) as client:
            resp = client.get("https://music.apple.com")
            resp.raise_for_status()
            matches = regex.findall(r"/assets/index~[^/\"']+\.js", resp.text)
            if not matches:
                # fallback: broader asset pattern used by some locales/builds
                matches = regex.findall(r"/assets/index[^\"']*?\.js", resp.text)
            if not matches:
                raise RuntimeError(
                    "已打开 music.apple.com，但未能从页面解析 index JS（页面结构可能已变更）。"
                )
            index_js_uri = matches[0]
            if not index_js_uri.startswith("http"):
                js_url = "https://music.apple.com" + index_js_uri
            else:
                js_url = index_js_uri
            js_resp = client.get(js_url)
            js_resp.raise_for_status()
            found = regex.search(
                r"(eyJ[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+)",
                js_resp.text,
            )
            if not found:
                raise RuntimeError("已下载 Apple Music 脚本，但未找到 developer token。")
            self.token = found.group(1)

    def _set_token(self, attempts: int = 3):
        """Fetch developer token from music.apple.com (uses proxy when configured)."""
        last_exc: BaseException | None = None
        for i in range(max(1, attempts)):
            try:
                self._fetch_token_once()
                return
            except (httpx.HTTPError, SSLError, OSError, RuntimeError) as exc:
                last_exc = exc
                it(GlobalLogger).logger.warning(
                    f"获取 Apple Music token 失败 ({i + 1}/{attempts}): {exc}"
                )
        raise RuntimeError(format_apple_network_error(last_exc or RuntimeError("unknown")))

    def ensure_token(self, force: bool = False):
        if self._token_ready and self.token and not force:
            return
        if not force and self._load_token_cache():
            self._rebuild_client()
            self._token_ready = True
            it(GlobalLogger).logger.info("已使用本地缓存的 Apple Music developer token")
            return
        self._set_token()
        self._save_token_cache()
        self._rebuild_client()
        self._token_ready = True

    # DO NOT REMOVE IT
    def init(self):
        """Eager token fetch used at GUI/CLI startup."""
        self.ensure_token(force=False)

    @retry(retry=retry_if_exception_type((httpx.HTTPError, SSLError, FileNotFoundError)),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime),
           before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def _request(self, *args, **kwargs):
        if not self._token_ready or not self.token:
            await asyncio.to_thread(self.ensure_token)
        async with self.request_lock:
            return await self.client.request(*args, **kwargs)

    @retry(retry=retry_if_exception_type((httpx.HTTPError, SSLError, FileNotFoundError)),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime),
           before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def _download_song_internal(self, url: str) -> bytes:
        result = BytesIO()
        timeout = httpx.Timeout(15.0, read=60.0, connect=15.0, pool=20.0)
        async with httpx.AsyncClient(transport=AsyncCustomHost(NameSolver()), timeout=timeout) as client:
            async with client.stream('GET', url) as response:
                total = int(response.headers.get("Content-Length") if response.headers.get("Content-Length")
                            else response.headers.get("X-Apple-MS-Content-Length"))
                async for chunk in response.aiter_bytes():
                    it(Measurer).record_download(len(chunk))
                    result.write(chunk)
            if len(result.getvalue()) != total:
                raise httpx.HTTPError
            return result.getvalue()

    async def download_song(self, url: str) -> bytes:
        async with self.download_lock:
            return await self._download_song_internal(url)

    async def get_album_info(self, album_id: str, storefront: str, lang: str):
        req = await self._request("GET",
                                  f"https://amp-api.music.apple.com/v1/catalog/{storefront}/albums/{album_id}",
                                  params={"omit[resource]": "autos", "include": "tracks,artists,record-labels",
                                          "include[songs]": "artists", "fields[artists]": "name",
                                          "fields[albums:albums]": "artistName,artwork,name,releaseDate,url",
                                          "fields[record-labels]": "name", "l": lang})
        album_info_obj = AlbumMeta.model_validate(req.json())
        all_tracks = await self.get_album_tracks(album_id, storefront, lang)
        album_info_obj.data[0].relationships.tracks.data = all_tracks
        return album_info_obj

    async def get_album_tracks(self, album_id: str, storefront: str, lang: str, offset: int = 0):
        req = await self._request("GET",
                                  f"https://amp-api.music.apple.com/v1/catalog/{storefront}/albums/{album_id}/tracks?offset={offset}")
        album_info_obj = AlbumTracks.model_validate(req.json())
        tracks = album_info_obj.data
        if album_info_obj.next:
            next_tracks = await self.get_album_tracks(album_id, storefront, lang, offset + 300)
            tracks.extend(next_tracks)
        return tracks

    async def get_playlist_info_and_tracks(self, playlist_id: str, storefront: str, lang: str):
        resp = await self._request("GET",
                                   f"https://amp-api.music.apple.com/v1/catalog/{storefront}/playlists/{playlist_id}",
                                   params={"l": lang})
        playlist_info_obj = PlaylistInfo.model_validate(resp.json())
        all_tracks = await self.get_playlist_tracks(playlist_id, storefront, lang)
        playlist_info_obj.data[0].relationships.tracks.data = all_tracks
        return playlist_info_obj

    async def get_playlist_tracks(self, playlist_id: str, storefront: str, lang: str, offset: int = 0):
        resp = await self._request(
            "GET",
            f"https://amp-api.music.apple.com/v1/catalog/{storefront}/playlists/{playlist_id}/tracks",
            params={"include": "catalog", "l": lang, "offset": offset, "limit": 100},
        )
        payload = resp.json()
        playlist_tracks = PlaylistTracks.model_validate(payload)
        raw_by_id = {item.get("id"): item for item in payload.get("data", []) if item.get("id")}
        tracks = []
        for track in playlist_tracks.data or []:
            raw_track = raw_by_id.get(track.id, {})
            catalog_id = self._catalog_id_from_track_data(raw_track)
            if catalog_id:
                track.catalog_id = catalog_id
            tracks.append(track)
        if playlist_tracks.next:
            tracks.extend(
                await self.get_playlist_tracks(playlist_id, storefront, lang, offset + len(tracks)),
            )
        return tracks

    def set_music_user_token(self, token: str):
        self._music_user_token = (token or "").strip()

    async def get_music_user_token(self) -> str:
        return self._music_user_token

    @retry(retry=retry_if_exception_type((httpx.HTTPError, SSLError, FileNotFoundError)),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime),
           before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def _request_library(self, method: str, url: str, **kwargs):
        if not self._music_user_token:
            raise ValueError("缺少 Music-User-Token，无法访问资料库歌单")
        headers = kwargs.pop("headers", {})
        headers["Music-User-Token"] = self._music_user_token
        async with self.request_lock:
            return await self.client.request(method, url, headers=headers, **kwargs)

    @staticmethod
    def _catalog_id_from_track_data(track_data: dict):
        from src.utils import song_id_from_apple_music_url

        if not track_data:
            return None
        catalog = track_data.get("relationships", {}).get("catalog", {}).get("data") or []
        if catalog and catalog[0].get("id"):
            return str(catalog[0]["id"])
        play_params = track_data.get("attributes", {}).get("playParams") or {}
        if play_params.get("catalogId"):
            return str(play_params["catalogId"])
        if play_params.get("id") and str(play_params.get("kind", "")).lower() == "song":
            raw = str(play_params["id"])
            if raw.isdigit():
                return raw
        track_url = track_data.get("attributes", {}).get("url") or ""
        url_id = song_id_from_apple_music_url(track_url)
        if url_id:
            return url_id
        raw_id = track_data.get("id")
        if raw_id and str(raw_id).isdigit():
            return str(raw_id)
        return None

    async def get_library_playlist_info_and_tracks(self, playlist_id: str, music_token: str, lang: str):
        self.set_music_user_token(music_token)
        resp = await self._request_library(
            "GET",
            f"https://amp-api.music.apple.com/v1/me/library/playlists/{playlist_id}",
            params={"l": lang},
        )
        playlist_info_obj = PlaylistInfo.model_validate(resp.json())
        playlist_info_obj.data[0].relationships.tracks.data = await self.get_library_playlist_tracks(
            playlist_id, lang,
        )
        return playlist_info_obj

    async def get_library_playlist_tracks(self, playlist_id: str, lang: str, offset: int = 0):
        resp = await self._request_library(
            "GET",
            f"https://amp-api.music.apple.com/v1/me/library/playlists/{playlist_id}/tracks",
            params={"include": "catalog", "l": lang, "offset": offset, "limit": 100},
        )
        payload = resp.json()
        playlist_tracks = PlaylistTracks.model_validate(payload)
        raw_by_id = {item.get("id"): item for item in payload.get("data", []) if item.get("id")}
        tracks = []
        for track in playlist_tracks.data or []:
            raw_track = raw_by_id.get(track.id, {})
            catalog_id = self._catalog_id_from_track_data(raw_track)
            if catalog_id:
                track.catalog_id = catalog_id
            tracks.append(track)
        if playlist_tracks.next:
            tracks.extend(await self.get_library_playlist_tracks(playlist_id, lang, offset + 100))
        return tracks

    async def get_library_album(self, album_id: str, lang: str):
        resp = await self._request_library(
            "GET",
            f"https://amp-api.music.apple.com/v1/me/library/albums/{album_id}",
            params={"l": lang},
        )
        return AlbumMeta.model_validate(resp.json())

    async def get_library_album_tracks(self, album_id: str, lang: str, offset: int = 0):
        resp = await self._request_library(
            "GET",
            f"https://amp-api.music.apple.com/v1/me/library/albums/{album_id}/tracks",
            params={"include": "catalog", "l": lang, "offset": offset, "limit": 100},
        )
        payload = resp.json()
        album_tracks = PlaylistTracks.model_validate(payload)
        raw_by_id = {item.get("id"): item for item in payload.get("data", []) if item.get("id")}
        tracks = []
        for track in album_tracks.data or []:
            raw_track = raw_by_id.get(track.id, {})
            catalog_id = self._catalog_id_from_track_data(raw_track)
            if catalog_id:
                track.catalog_id = catalog_id
            tracks.append(track)
        if album_tracks.next:
            tracks.extend(await self.get_library_album_tracks(album_id, lang, offset + 100))
        return tracks

    async def resolve_library_song(self, library_song_id: str, lang: str) -> tuple[str, str]:
        resp = await self._request_library(
            "GET",
            f"https://amp-api.music.apple.com/v1/me/library/songs/{library_song_id}",
            params={"include": "catalog", "l": lang},
        )
        payload = resp.json()
        if not payload.get("data"):
            raise ValueError(f"资料库歌曲不存在: {library_song_id}")
        item = payload["data"][0]
        catalog_id = self._catalog_id_from_track_data(item)
        if not catalog_id:
            raise ValueError(f"无法解析资料库歌曲的 catalog ID: {library_song_id}")
        title = item.get("attributes", {}).get("name") or "Unknown"
        return catalog_id, title

    async def get_cover(self, url: str, cover_format: str, cover_size: str):
        async with self.request_lock:
            formatted_url = regex.sub('bb.jpg', f'bb.{cover_format}', url)
            req = await self._request("GET", formatted_url.replace("{w}x{h}", cover_size))
            return req.content

    async def get_song_info(self, song_id: str, storefront: str, lang: str):
        req = await self._request("GET", f"https://amp-api.music.apple.com/v1/catalog/{storefront}/songs/{song_id}",
                                  params={"extend": "extendedAssetUrls", "include": "albums,explicit", "l": lang})
        song_data_obj = SongData.model_validate(req.json())
        for data in song_data_obj.data:
            if data.id == song_id:
                return data
        return None

    async def song_exist(self, song_id: str, storefront: str):
        req = await self._request("HEAD", f"https://amp-api.music.apple.com/v1/catalog/{storefront}/songs/{song_id}")
        if req.status_code == 200:
            return True
        return False

    async def album_exist(self, album_id: str, storefront: str):
        req = await self._request("HEAD", f"https://amp-api.music.apple.com/v1/catalog/{storefront}/albums/{album_id}")
        if req.status_code == 200:
            return True
        return False

    async def get_albums_from_artist(self, artist_id: str, storefront: str, lang: str, offset: int = 0):
        resp = await self._request("GET",
                                   f"https://amp-api.music.apple.com/v1/catalog/{storefront}/artists/{artist_id}/albums",
                                   params={"l": lang, "offset": offset})
        artist_album = ArtistAlbums.model_validate(resp.json())
        albums = [album.attributes.url for album in artist_album.data]
        if artist_album.next:
            next_albums = await self.get_albums_from_artist(artist_id, storefront, lang, offset + 25)
            albums.extend(next_albums)
        return list(set(albums))

    async def get_songs_from_artist(self, artist_id: str, storefront: str, lang: str, offset: int = 0):
        resp = await self._request("GET",
                                   f"https://amp-api.music.apple.com/v1/catalog/{storefront}/artists/{artist_id}/songs",
                                   params={"l": lang, "offset": offset})
        artist_song = ArtistSongs.model_validate(resp.json())
        songs = [song.attributes.url for song in artist_song.data]
        if artist_song.next:
            next_songs = await self.get_songs_from_artist(artist_id, storefront, lang, offset + 20)
            songs.extend(next_songs)
        return list(set(songs))

    async def get_artist_info(self, artist_id: str, storefront: str, lang: str):
        resp = await self._request("GET",
                                   f"https://amp-api.music.apple.com/v1/catalog/{storefront}/artists/{artist_id}",
                                   params={"l": lang})
        return ArtistInfo.model_validate(resp.json())

    async def download_m3u8(self, m3u8_url: str) -> str:
        resp = await self._request("GET", m3u8_url)
        return resp.text

    async def get_real_url(self, url: str):
        req = await self._request("GET", url, follow_redirects=True)
        return str(req.url)

    async def resolve_catalog_track_entry(
        self, track, default_storefront: str, *, allow_http_resolve: bool = True,
    ) -> Optional[tuple[str, str, str, str]]:
        """Resolve playlist/album track to (song_id, storefront, title, artist).

        Prefer local metadata (URL / playParams / id) — avoid HTTP redirects.
        Network resolve is last resort and can be disabled for bulk speed.
        """
        from src.url import AppleMusicURL, URLType
        from src.utils import parse_song_from_apple_url, resolve_catalog_track, storefront_from_apple_music_url

        attrs = getattr(track, "attributes", None)
        title = getattr(attrs, "name", None) or ""
        artist = getattr(attrs, "artistName", None) or ""
        track_url = getattr(attrs, "url", None) or ""

        # 1) Sync catalog fields (fast path for most album/playlist tracks)
        resolved = resolve_catalog_track(track, default_storefront)
        if resolved:
            return resolved[0], resolved[1], title, artist

        catalog_id = getattr(track, "catalog_id", None)
        if catalog_id and str(catalog_id).isdigit() and not str(catalog_id).startswith("i."):
            sf = storefront_from_apple_music_url(track_url, default_storefront) if track_url else default_storefront
            return str(catalog_id), sf, title, artist

        # 2) Local URL parse
        if track_url:
            via_local = parse_song_from_apple_url(track_url)
            if via_local:
                return via_local[0], via_local[1], title, artist

        # 3) HTTP redirect only when necessary (slow)
        if allow_http_resolve and track_url:
            try:
                real_url = await self.get_real_url(track_url)
                song = parse_song_from_apple_url(real_url)
                if song:
                    return song[0], song[1], title, artist
                am_url = AppleMusicURL.parse_url(real_url)
                if am_url and am_url.type == URLType.Song:
                    return am_url.id, am_url.storefront, title, artist
            except Exception:
                pass

        return None

    async def get_album_by_upc(self, upc: str, storefront: str):
        req = await self._request("GET", f"https://amp-api.music.apple.com/v1/catalog/{storefront}/albums",
                                  params={"filter[upc]": upc})
        resp = req.json()
        try:
            if resp["data"]:
                return req.json()
            else:
                return None
        except KeyError:
            return None

    async def exist_on_storefront_by_song_id(self, song_id: str, storefront: str, check_storefront: str):
        if await self.song_exist(song_id, storefront):
            return True
        if storefront.upper() != check_storefront.upper():
            return await self.song_exist(song_id, check_storefront)
        return False

    async def exist_on_storefront_by_album_id(self, album_id: str, storefront: str, check_storefront: str):
        if await self.album_exist(album_id, storefront):
            return True
        if storefront.upper() != check_storefront.upper():
            return await self.album_exist(album_id, check_storefront)
        return False


class APICreator(AbstractCreator):
    targets = (
        CreateTargetInfo("src.api", "WebAPI"),
    )

    @staticmethod
    def available() -> bool:
        return exists_module("src.api")

    @staticmethod
    def create(create_type: Type[WebAPI]) -> WebAPI:
        return create_type(it(Config).download.proxy, it(Config).download.parallelNum)
