import asyncio
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional

from creart import it
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.api import WebAPI
from src.config import Config
from src.exceptions import CodecNotFoundException, SongNotPassIntegrityCheckException
from src.flags import Flags
from src.grpc.manager import WrapperManager
from src.legacy.decrypt import WidevineDecrypt
from src.legacy.mp4 import decrypt as legacy_decrypt
from src.legacy.mp4 import extract_media as legacy_extract_media
from src.logger import RipLogger
from src.measurer import Measurer
from src.metadata import SongMetadata
from src.models import PlaylistInfo
from src.mp4 import extract_media, extract_song, encapsulate, write_metadata, fix_encapsulate, fix_esds_box, \
    check_song_integrity
from src.save import save
from src.task import Task, Status
from src.types import Codec, ParentDoneHandler, DownloadPathContext
from src.url import Song, Album, URLType, Playlist, LibraryPlaylist, LibraryAlbum, LibrarySong
from src.utils import get_codec_from_codec_id, check_song_existence, check_song_exists, if_raw_atmos, \
    check_album_existence, hidden_subprocess_kwargs, run_sync, safely_create_task, language_exist, query_language


def _task_key(adam_id: str, codec: str = "") -> str:
    return f"{adam_id}:{codec}" if codec else adam_id


@dataclass
class StagedSong:
    """Encrypted audio already downloaded; ready for decrypt/save pass."""
    task: Task
    url: Song
    raw_song: bytes
    local_codec_hint: str = ""


class DownloadManager:
    def __init__(self):
        self.adam_id_task_mapping: Dict[str, Task] = {}
        self.finished_snapshots: Dict[str, dict] = {}
        self.task_lock = asyncio.Semaphore(it(Config).download.maxRunningTasks)

    def clear_finished_snapshots(self):
        self.finished_snapshots.clear()

    def record_finished(self, task: Task):
        title = task.metadata.title if task.metadata else task.adamId
        artist = task.metadata.artist if task.metadata else ""
        self.finished_snapshots[task.adamId] = {
            "id": task.adamId,
            "codec": task.codec,
            "title": title,
            "artist": artist,
            "status": task.status.value,
            "error": str(task.error) if task.error else "",
        }

    async def register_task(self, task: Task):
        key = _task_key(task.adamId, task.codec)
        self.adam_id_task_mapping[key] = task
        await self.task_lock.acquire()
        it(Measurer).record_task_start()

    async def unregister_task(self, task: Task):
        self.record_finished(task)
        key = _task_key(task.adamId, task.codec)
        if key in self.adam_id_task_mapping:
            del self.adam_id_task_mapping[key]
            self.task_lock.release()
            it(Measurer).record_task_finish()

    def get_task(self, adam_id: str, codec: str = "") -> Optional[Task]:
        if codec:
            return self.adam_id_task_mapping.get(_task_key(adam_id, codec))
        task = self.adam_id_task_mapping.get(_task_key(adam_id, codec))
        if task:
            return task
        for mapped in self.adam_id_task_mapping.values():
            if mapped.adamId == adam_id:
                return mapped
        return None

    def list_tasks(self) -> list[Task]:
        return list(self.adam_id_task_mapping.values())


class Ripper:
    def __init__(self):
        self.download_manager = DownloadManager()
        self._cancel_all = False
        self._status_listeners: list = []

    def on_task_status(self, callback):
        self._status_listeners.append(callback)

    def _notify_task_status(self, adam_id: str, status: Status):
        for cb in list(self._status_listeners):
            try:
                cb(adam_id, status)
            except Exception:
                pass

    def _bind_task_status_events(self, task: Task):
        original_update = task.update_status

        def update_status(status: Status):
            original_update(status)
            self._notify_task_status(task.adamId, status)

        task.update_status = update_status

    def request_cancel_all(self):
        self._cancel_all = True
        for task in self.download_manager.list_tasks():
            task.cancelled = True
            task.update_status(Status.FAILED)
            task.error = Exception("任务已取消")

    def clear_cancel(self):
        self._cancel_all = False

    def _check_cancelled(self, task: Task):
        if self._cancel_all or task.cancelled:
            raise asyncio.CancelledError("任务已取消")

    async def rip_song(
        self,
        url: Song,
        codec: str,
        flags: Flags = Flags(),
        parent_done: ParentDoneHandler = None,
        playlist: PlaylistInfo = None,
        path_context: DownloadPathContext = None,
        timeout_sec: int = 0,
    ) -> tuple[Status, Optional[str]]:
        if self.download_manager.get_task(url.id, codec):
            if parent_done:
                # If task already exists, we must notify the parent that this "sub-task" is considered handled/skipped
                # to prevent the parent from waiting indefinitely.
                await parent_done.try_done()
            return Status.DONE, None

        task = Task(
            adamId=url.id,
            codec=codec,
            parentDone=parent_done,
            playlist=playlist,
            path_context=path_context,
        )

        # Initialize Logger
        task.logger = RipLogger(URLType.Song, task.adamId)

        try:
            await self.download_manager.register_task(task)
            self._bind_task_status_events(task)
            self._check_cancelled(task)

            # Fetch Metadata
            raw_metadata = await it(WebAPI).get_song_info(task.adamId, url.storefront, flags.language)
            self._check_cancelled(task)
            album_data = await it(WebAPI).get_album_info(raw_metadata.relationships.albums.data[0].id, url.storefront,
                                                         flags.language)
            task.metadata = SongMetadata.parse_from_song_data(raw_metadata)
            task.metadata.parse_from_album_data(album_data)

            if task.path_context is None and playlist is None:
                task.path_context = DownloadPathContext(
                    kind="song",
                    container_name=task.metadata.title or "Unknown",
                )

            # Update Logger with metadata
            task.logger.set_fullname(task.metadata.artist, task.metadata.title)
            task.logger.create()

            # Check Language
            if it(Config).region.languageNotExistWarning and not language_exist(url.storefront, flags.language):
                default_language, _ = query_language(url.storefront)
                task.logger.language_not_exist(url.storefront, flags.language, default_language)

            # Check Existence on Apple Music
            if not await check_song_existence(url.id, url.storefront):
                task.logger.not_exist()
                task.update_status(Status.FAILED)
                task.error = Exception(
                    f"Song not found on Apple Music (storefront={url.storefront}, id={url.id})",
                )
                return task.status, str(task.error)

            # Get Cover and Lyrics
            task.metadata.cover = await it(WebAPI).get_cover(task.metadata.cover_url,
                                                             it(Config).download.coverFormat,
                                                             it(Config).download.coverSize)

            if raw_metadata.attributes.hasTimeSyncedLyrics:
                task.metadata.lyrics = await it(WrapperManager).lyrics(task.adamId, flags.language, url.storefront)

            if playlist:
                task.metadata.set_playlist_index(playlist.songIdIndexMapping.get(url.id))

            # Check Local Existence
            if not flags.force_save and check_song_exists(
                task.metadata, codec, playlist, task.path_context,
            ):
                task.logger.already_exist()
                task.update_status(Status.DONE)
                return task.status, None

            # Get M3U8
            m3u8_url = await self._get_m3u8_url(task, codec, raw_metadata)

            if codec == Codec.AAC_LEGACY or (
                    it(Config).download.codecAlternative and not raw_metadata.attributes.extendedAssetUrls.enhancedHls and Codec.AAC_LEGACY in it(
                    Config).download.codecPriority):
                await self._rip_song_legacy(task, timeout_sec)
                return task.status, str(task.error) if task.error else None

            if not m3u8_url:
                task.logger.logger.error("Lossless audio does not exist")
                task.update_status(Status.FAILED)
                task.error = Exception("Lossless audio does not exist")
                return task.status, str(task.error)

            try:
                task.m3u8Info = await extract_media(m3u8_url, codec, task)
            except CodecNotFoundException:
                task.logger.audio_not_exist()
                task.update_status(Status.FAILED)
                task.error = CodecNotFoundException(f"Audio codec '{codec}' not found")
                return task.status, str(task.error)

            task.logger.selected_codec(task.m3u8Info.codec_id)
            if all([bool(task.m3u8Info.bit_depth), bool(task.m3u8Info.sample_rate)]):
                task.metadata.set_bit_depth_and_sample_rate(task.m3u8Info.bit_depth, task.m3u8Info.sample_rate)
                # Check existence again with precise metadata
                if not flags.force_save and check_song_exists(
                    task.metadata, codec, playlist, task.path_context,
                ):
                    task.logger.already_exist()
                    task.update_status(Status.DONE)
                    return task.status, None

            # Wait in queue — download then decrypt (same song path)
            task.logger.logger.info("Waiting for available download streams...")
            async with it(WebAPI).download_lock:
                async def _phase2():
                    staged = await self._download_raw_for_task(task)
                    # Outer finally unregisters the task
                    await self._decrypt_staged_and_save(staged, unregister=False)

                if timeout_sec > 0:
                    await asyncio.wait_for(_phase2(), timeout=timeout_sec)
                else:
                    await _phase2()

        except asyncio.TimeoutError:
            task.logger.logger.warning("Task processing timed out after waiting in queue")
            task.update_status(Status.FAILED)
            task.error = Exception("Task execution timed out")

        except Exception as e:
            task.logger.logger.exception(f"Error processing song: {e}")
            task.update_status(Status.FAILED)
            task.error = e
        except asyncio.CancelledError:
            task.logger.logger.warning("任务已取消")
            task.update_status(Status.FAILED)
            task.error = Exception("任务已取消")
            return task.status, str(task.error)
        finally:
            await self.download_manager.unregister_task(task)
            task.update_status(task.status)  # Ensure status is set
            if task.parentDone:
                await task.parentDone.try_done()
        return task.status, str(task.error) if task.error else None

    async def rip_cached_song(
        self,
        candidate,
        storefront: str,
        flags: Flags = Flags(),
        path_context: DownloadPathContext = None,
        timeout_sec: int = 0,
    ) -> tuple[Status, Optional[str]]:
        from src.cache_import import IMPORT_READY, build_cached_staged_payload

        if candidate.import_status != IMPORT_READY:
            return Status.FAILED, candidate.note or candidate.import_status
        if not candidate.adam_id:
            return Status.FAILED, "缓存条目缺少可识别的曲目 ID"
        if not candidate.codec:
            return Status.FAILED, "缓存条目缺少可识别的编码"
        if self.download_manager.get_task(candidate.adam_id, candidate.codec):
            return Status.DONE, None

        task = Task(
            adamId=candidate.adam_id,
            codec=candidate.codec,
            path_context=path_context,
        )
        task.logger = RipLogger(URLType.Song, task.adamId)

        try:
            await self.download_manager.register_task(task)
            self._bind_task_status_events(task)
            self._check_cancelled(task)

            raw_metadata = await it(WebAPI).get_song_info(task.adamId, storefront, flags.language)
            if not raw_metadata:
                task.update_status(Status.FAILED)
                task.error = Exception(f"Song not found on Apple Music (storefront={storefront}, id={task.adamId})")
                return task.status, str(task.error)

            album_data = await it(WebAPI).get_album_info(
                raw_metadata.relationships.albums.data[0].id, storefront, flags.language,
            )
            task.metadata = SongMetadata.parse_from_song_data(raw_metadata)
            task.metadata.parse_from_album_data(album_data)
            if task.path_context is None:
                task.path_context = DownloadPathContext(
                    kind="song",
                    container_name=task.metadata.title or candidate.track_title or "Unknown",
                    parent_container="本地缓存",
                )
            task.logger.set_fullname(task.metadata.artist, task.metadata.title)
            task.logger.create()

            if it(Config).region.languageNotExistWarning and not language_exist(storefront, flags.language):
                default_language, _ = query_language(storefront)
                task.logger.language_not_exist(storefront, flags.language, default_language)

            if not await check_song_existence(task.adamId, storefront):
                task.logger.not_exist()
                task.update_status(Status.FAILED)
                task.error = Exception(f"Song not found on Apple Music (storefront={storefront}, id={task.adamId})")
                return task.status, str(task.error)

            task.metadata.cover = await it(WebAPI).get_cover(
                task.metadata.cover_url,
                it(Config).download.coverFormat,
                it(Config).download.coverSize,
            )
            if raw_metadata.attributes.hasTimeSyncedLyrics:
                task.metadata.lyrics = await it(WrapperManager).lyrics(task.adamId, flags.language, storefront)

            raw_song, task.m3u8Info = await run_sync(build_cached_staged_payload, candidate)
            if all([bool(task.m3u8Info.bit_depth), bool(task.m3u8Info.sample_rate)]):
                task.metadata.set_bit_depth_and_sample_rate(
                    task.m3u8Info.bit_depth, task.m3u8Info.sample_rate,
                )

            if not flags.force_save and check_song_exists(
                task.metadata, candidate.codec, None, task.path_context,
            ):
                task.logger.already_exist()
                task.update_status(Status.DONE)
                return task.status, None

            staged = StagedSong(
                task=task,
                url=Song(id=task.adamId, storefront=storefront, url="", type=URLType.Song),
                raw_song=raw_song,
                local_codec_hint=candidate.codec,
            )

            async def _phase2():
                await self._decrypt_staged_and_save(staged, unregister=False)

            if timeout_sec > 0:
                await asyncio.wait_for(_phase2(), timeout=timeout_sec)
            else:
                await _phase2()

        except asyncio.TimeoutError:
            task.logger.logger.warning("Cached task processing timed out")
            task.update_status(Status.FAILED)
            task.error = Exception("Cached task execution timed out")
        except asyncio.CancelledError:
            task.logger.logger.warning("任务已取消")
            task.update_status(Status.FAILED)
            task.error = Exception("任务已取消")
            return task.status, str(task.error)
        except Exception as e:
            task.logger.logger.exception(f"Error processing cached song: {e}")
            task.update_status(Status.FAILED)
            task.error = e
        finally:
            await self.download_manager.unregister_task(task)
            task.update_status(task.status)
        return task.status, str(task.error) if task.error else None

    async def prepare_song_for_stage(
        self,
        url: Song,
        codec: str,
        flags: Flags = Flags(),
        playlist: PlaylistInfo = None,
        path_context: DownloadPathContext = None,
    ) -> tuple[Status, Optional[str], Optional[Task], Optional[object]]:
        """Metadata + m3u8 only (no download). Returns (status, err, task, raw_metadata)."""
        if self.download_manager.get_task(url.id, codec):
            return Status.DONE, None, None, None

        task = Task(
            adamId=url.id,
            codec=codec,
            playlist=playlist,
            path_context=path_context,
        )
        task.logger = RipLogger(URLType.Song, task.adamId)
        try:
            await self.download_manager.register_task(task)
            self._bind_task_status_events(task)
            self._check_cancelled(task)

            raw_metadata = await it(WebAPI).get_song_info(task.adamId, url.storefront, flags.language)
            album_data = await it(WebAPI).get_album_info(
                raw_metadata.relationships.albums.data[0].id, url.storefront, flags.language,
            )
            task.metadata = SongMetadata.parse_from_song_data(raw_metadata)
            task.metadata.parse_from_album_data(album_data)
            if task.path_context is None and playlist is None:
                task.path_context = DownloadPathContext(
                    kind="song", container_name=task.metadata.title or "Unknown",
                )
            task.logger.set_fullname(task.metadata.artist, task.metadata.title)
            task.logger.create()

            if not await check_song_existence(url.id, url.storefront):
                task.logger.not_exist()
                task.update_status(Status.FAILED)
                task.error = Exception(f"Song not found ({url.storefront}/{url.id})")
                await self.download_manager.unregister_task(task)
                return task.status, str(task.error), None, None

            task.metadata.cover = await it(WebAPI).get_cover(
                task.metadata.cover_url,
                it(Config).download.coverFormat,
                it(Config).download.coverSize,
            )
            if raw_metadata.attributes.hasTimeSyncedLyrics:
                task.metadata.lyrics = await it(WrapperManager).lyrics(
                    task.adamId, flags.language, url.storefront,
                )
            if playlist:
                task.metadata.set_playlist_index(playlist.songIdIndexMapping.get(url.id))

            if not flags.force_save and check_song_exists(
                task.metadata, codec, playlist, task.path_context,
            ):
                task.logger.already_exist()
                task.update_status(Status.DONE)
                await self.download_manager.unregister_task(task)
                return Status.DONE, None, None, None

            m3u8_url = await self._get_m3u8_url(task, codec, raw_metadata)
            if codec == Codec.AAC_LEGACY or (
                it(Config).download.codecAlternative
                and not raw_metadata.attributes.extendedAssetUrls.enhancedHls
                and Codec.AAC_LEGACY in it(Config).download.codecPriority
            ):
                # Legacy path stays one-shot
                await self._rip_song_legacy(task, 0)
                st, err = task.status, str(task.error) if task.error else None
                await self.download_manager.unregister_task(task)
                return st, err, None, None

            if not m3u8_url:
                task.update_status(Status.FAILED)
                task.error = Exception("Lossless audio does not exist")
                await self.download_manager.unregister_task(task)
                return task.status, str(task.error), None, None

            try:
                task.m3u8Info = await extract_media(m3u8_url, codec, task)
            except CodecNotFoundException as e:
                task.update_status(Status.FAILED)
                task.error = e
                await self.download_manager.unregister_task(task)
                return task.status, str(e), None, None

            task.logger.selected_codec(task.m3u8Info.codec_id)
            if all([bool(task.m3u8Info.bit_depth), bool(task.m3u8Info.sample_rate)]):
                task.metadata.set_bit_depth_and_sample_rate(
                    task.m3u8Info.bit_depth, task.m3u8Info.sample_rate,
                )
                if not flags.force_save and check_song_exists(
                    task.metadata, codec, playlist, task.path_context,
                ):
                    task.logger.already_exist()
                    task.update_status(Status.DONE)
                    await self.download_manager.unregister_task(task)
                    return Status.DONE, None, None, None

            return Status.WAITING, None, task, raw_metadata
        except Exception as e:
            task.update_status(Status.FAILED)
            task.error = e
            try:
                await self.download_manager.unregister_task(task)
            except Exception:
                pass
            return Status.FAILED, str(e), None, None

    async def _download_raw_for_task(self, task: Task) -> StagedSong:
        self._check_cancelled(task)
        task.logger.downloading()
        task.update_status(Status.DOWNLOADING)
        raw_song = await it(WebAPI)._download_song_internal(task.m3u8Info.uri)
        return StagedSong(
            task=task,
            url=Song(id=task.adamId, storefront="", url="", type=URLType.Song),
            raw_song=raw_song,
            local_codec_hint=get_codec_from_codec_id(task.m3u8Info.codec_id),
        )

    async def stage_download_song(
        self,
        url: Song,
        codec: str,
        flags: Flags = Flags(),
        playlist: PlaylistInfo = None,
        path_context: DownloadPathContext = None,
    ) -> tuple[Status, Optional[str], Optional[StagedSong]]:
        """Phase A: metadata + CDN download only (holds download_lock)."""
        status, err, task, _meta = await self.prepare_song_for_stage(
            url, codec, flags, playlist, path_context,
        )
        if task is None:
            return status, err, None
        try:
            async with it(WebAPI).download_lock:
                staged = await self._download_raw_for_task(task)
            # Stay registered until decrypt finishes
            return Status.DOWNLOADING, None, staged
        except Exception as e:
            task.update_status(Status.FAILED)
            task.error = e
            await self.download_manager.unregister_task(task)
            return Status.FAILED, str(e), None

    async def _decrypt_staged_and_save(
        self, staged: StagedSong, *, unregister: bool = True,
    ) -> tuple[Status, Optional[str]]:
        task = staged.task
        raw_song = staged.raw_song
        try:
            self._check_cancelled(task)
            task.logger.decrypting()
            task.update_status(Status.DECRYPTING)

            task.info = await run_sync(
                extract_song, raw_song, get_codec_from_codec_id(task.m3u8Info.codec_id),
            )
            n_samples = len(task.info.samples)
            for i in range(n_samples):
                task.decrypted_samples_futures[i] = asyncio.get_running_loop().create_future()

            window = 12 if n_samples > 48 else max(4, min(8, n_samples))
            decrypted_samples = [None] * n_samples
            next_send = 0
            next_recv = 0
            inflight = {}

            async def _send(idx: int):
                sample = task.info.samples[idx]
                return await self.decrypt_sample_with_retry(
                    task.adamId,
                    task.m3u8Info.keys[sample.descIndex],
                    sample.data,
                    idx,
                )

            try:
                while next_recv < n_samples:
                    self._check_cancelled(task)
                    while next_send < n_samples and (next_send - next_recv) < window:
                        inflight[next_send] = asyncio.create_task(_send(next_send))
                        next_send += 1
                    done_idx = next_recv
                    decrypted_samples[done_idx] = await inflight.pop(done_idx)
                    next_recv += 1
                    if next_recv % 64 == 0:
                        await asyncio.sleep(0)
            except Exception:
                for t in list(inflight.values()):
                    t.cancel()
                raise

            local_codec = get_codec_from_codec_id(task.m3u8Info.codec_id)
            song_bytes = await run_sync(
                encapsulate, task.info, bytes().join(decrypted_samples),
                it(Config).download.atmosConventToM4a,
            )
            if not if_raw_atmos(local_codec, it(Config).download.atmosConventToM4a):
                if local_codec != Codec.EC3 and local_codec != Codec.AC3:
                    song_bytes = await run_sync(fix_encapsulate, song_bytes)
                song_bytes = await run_sync(
                    write_metadata, song_bytes, task.metadata,
                    it(Config).metadata.embedMetadata,
                    it(Config).download.coverFormat, task.info.params,
                )
                if local_codec in (Codec.AAC, Codec.AAC_DOWNMIX, Codec.AAC_BINAURAL):
                    song_bytes = await run_sync(fix_esds_box, task.info.raw, song_bytes)

            if not await run_sync(check_song_integrity, song_bytes):
                if it(Config).download.failedSongNotPassIntegrityCheck:
                    task.logger.failed_integrity(True)
                    task.update_status(Status.FAILED)
                    raise SongNotPassIntegrityCheckException("Integrity Check Failed")
                task.logger.failed_integrity(False)
                task.error = SongNotPassIntegrityCheckException("Integrity Check Warning")

            local_filename = await run_sync(
                save, song_bytes, local_codec, task.metadata, task.playlist, task.path_context,
            )
            task.logger.saved(local_filename)
            task.update_status(Status.DONE)
            if it(Config).download.afterDownloaded:
                command = it(Config).download.afterDownloaded.format(filename=local_filename)
                subprocess.Popen(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **hidden_subprocess_kwargs(),
                )
            return task.status, str(task.error) if task.error else None
        except Exception as e:
            task.update_status(Status.FAILED)
            task.error = e
            return Status.FAILED, str(e)
        finally:
            if unregister:
                await self.download_manager.unregister_task(task)
            task.update_status(task.status)

    async def finish_decrypt_staged(self, staged: StagedSong) -> tuple[Status, Optional[str]]:
        """Phase B: decrypt + encapsulate + save (no CDN)."""
        return await self._decrypt_staged_and_save(staged, unregister=True)

    async def _get_m3u8_url(self, task: Task, codec: str, raw_metadata) -> Optional[str]:
        if not raw_metadata.attributes.extendedAssetUrls:
            task.logger.audio_not_exist()
            return None

        m3u8_url = None
        if codec == Codec.ALAC and raw_metadata.attributes.extendedAssetUrls.enhancedHls:
            m3u8_url = await it(WrapperManager).m3u8(task.adamId)
        else:
            if codec != Codec.AAC_LEGACY:
                m3u8_url = raw_metadata.attributes.extendedAssetUrls.enhancedHls

        return m3u8_url

    async def _rip_song_legacy(self, task: Task, timeout_sec: int = 0):
        # Simplified legacy ripping integrated into the flow
        try:
            task.m3u8Info = await legacy_extract_media(await it(WrapperManager).webPlayback(task.adamId))

            async with it(WebAPI).download_lock:
                async def _phase2():
                    task.logger.downloading()
                    task.update_status(Status.DOWNLOADING)
                    raw_song = await it(WebAPI)._download_song_internal(task.m3u8Info.uri)
                    task.info = await run_sync(extract_song, raw_song, Codec.AAC_LEGACY)
                    
                    task.logger.decrypting()
                    task.update_status(Status.DECRYPTING)
                    wvDecrypt = WidevineDecrypt()
                    challenge = wvDecrypt.generate_challenge(task.m3u8Info.keys[0].split(",")[1])
                    wvLicense = await it(WrapperManager).license(adam_id=task.adamId, challenge=challenge,
                                                                 kid=task.m3u8Info.keys[0])
                    keys = wvDecrypt.generate_key(wvLicense)
                    song_bytes = await run_sync(legacy_decrypt, raw_song, keys[1].kid.hex, keys[1].key.hex())
        
                    song_bytes = await run_sync(write_metadata, song_bytes, task.metadata, it(Config).metadata.embedMetadata,
                                          it(Config).download.coverFormat, task.info.params)
        
                    if not await run_sync(check_song_integrity, song_bytes):
                        task.logger.failed_integrity(True)
        
                    local_filename = await run_sync(
                        save, song_bytes, Codec.AAC_LEGACY, task.metadata, task.playlist, task.path_context,
                    )
                    task.logger.saved(local_filename)
                    task.update_status(Status.DONE)
        
                    if it(Config).download.afterDownloaded:
                        command = it(Config).download.afterDownloaded.format(filename=local_filename)
                        subprocess.Popen(
                            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **hidden_subprocess_kwargs(),
                        )

                if timeout_sec > 0:
                    await asyncio.wait_for(_phase2(), timeout=timeout_sec)
                else:
                    await _phase2()

        except asyncio.TimeoutError:
            task.logger.logger.warning("Task processing timed out after waiting in queue")
            task.update_status(Status.FAILED)
            task.error = Exception("Legacy Task execution timed out")
        except Exception as e:
            task.logger.logger.exception(f"Legacy rip failed: {e}")
            task.update_status(Status.FAILED)
            task.error = e

    async def rip_album(
        self,
        url: Album,
        codec: str,
        flags: Flags = Flags(),
        parent_done: ParentDoneHandler = None,
        parent_container: str = None,
    ):
        album_info = await it(WebAPI).get_album_info(url.id, url.storefront, flags.language)
        logger = RipLogger(url.type, url.id)
        logger.set_fullname(album_info.data[0].attributes.artistName, album_info.data[0].attributes.name)

        logger.create()
        if not await check_album_existence(url.id, url.storefront):
            logger.not_exist()
            return

        async def on_children_done():
            logger.done()
            if parent_done:
                await parent_done.try_done()

        done_handler = ParentDoneHandler(len(album_info.data[0].relationships.tracks.data), on_children_done)
        album_ctx = DownloadPathContext(
            kind="album",
            container_name=album_info.data[0].attributes.name or "Album",
            parent_container=parent_container,
        )

        for track in album_info.data[0].relationships.tracks.data:
            row = await it(WebAPI).resolve_catalog_track_entry(track, url.storefront)
            if not row:
                continue
            song_id, track_sf, _, _ = row
            song = Song(id=song_id, storefront=track_sf, url="", type=URLType.Song)
            safely_create_task(self.rip_song(song, codec, flags, done_handler, path_context=album_ctx))

    async def rip_artist(self, url: Album, codec: str, flags: Flags = Flags()):
        artist_info = await it(WebAPI).get_artist_info(url.id, url.storefront, flags.language)
        artist_name = artist_info.data[0].attributes.name or "Artist"
        logger = RipLogger(url.type, url.id)
        logger.set_fullname(artist_name)

        logger.create()

        async def on_children_done():
            logger.done()

        if flags.include_participate_in_works:
            songs = await it(WebAPI).get_songs_from_artist(url.id, url.storefront, flags.language)
            done_handler = ParentDoneHandler(len(songs), on_children_done)
            for song_url in songs:
                song_ctx = DownloadPathContext(
                    kind="song",
                    container_name="",
                    parent_container=artist_name,
                )
                parsed = Song.parse_url(song_url)
                if parsed:
                    safely_create_task(
                        self.rip_song(parsed, codec, flags, done_handler, path_context=song_ctx),
                    )
        else:
            albums = await it(WebAPI).get_albums_from_artist(url.id, url.storefront, flags.language)
            done_handler = ParentDoneHandler(len(albums), on_children_done)
            for album_url in albums:
                parsed_album = Album.parse_url(album_url)
                if parsed_album:
                    safely_create_task(
                        self.rip_album(
                            parsed_album, codec, flags, done_handler, parent_container=artist_name,
                        ),
                    )

    async def rip_playlist(self, url: Playlist, codec: str, flags: Flags = Flags()):
        playlist_info = await it(WebAPI).get_playlist_info_and_tracks(url.id, url.storefront, flags.language)
        await self._rip_playlist_tracks(url.storefront, playlist_info, codec, flags)

    async def rip_library_playlist(self, url: LibraryPlaylist, codec: str, flags: Flags = Flags()):
        music_token = await it(WebAPI).get_music_user_token()
        if not music_token:
            raise ValueError("无法读取 Music-User-Token，请先在「Apple ID 登录」页登录并确保内核内账号有效")
        playlist_info = await it(WebAPI).get_library_playlist_info_and_tracks(
            url.id, music_token, flags.language,
        )
        await self._rip_playlist_tracks(
            url.storefront, playlist_info, codec, flags, playlist_kind="library_playlist",
        )

    async def rip_library_album(self, url: LibraryAlbum, codec: str, flags: Flags = Flags()):
        music_token = await it(WebAPI).get_music_user_token()
        if not music_token:
            raise ValueError("无法读取 Music-User-Token，请先在「Apple ID 登录」页登录并确保内核内账号有效")
        album_meta = await it(WebAPI).get_library_album(url.id, flags.language)
        tracks = await it(WebAPI).get_library_album_tracks(url.id, flags.language)
        album_name = album_meta.data[0].attributes.name or "Album"
        logger = RipLogger(URLType.LibraryAlbum, url.id)
        logger.set_fullname(album_meta.data[0].attributes.artistName or "", album_name)
        logger.create()

        resolved = []
        for track in tracks:
            row = await it(WebAPI).resolve_catalog_track_entry(track, url.storefront)
            if not row:
                continue
            song_id, track_sf, _, _ = row
            resolved.append((song_id, track_sf))

        async def on_children_done():
            logger.done()

        done_handler = ParentDoneHandler(len(resolved), on_children_done)
        album_ctx = DownloadPathContext(kind="library_album", container_name=album_name)
        for song_id, track_sf in resolved:
            song = Song(id=song_id, storefront=track_sf, url="", type=URLType.Song)
            safely_create_task(self.rip_song(song, codec, flags, done_handler, path_context=album_ctx))

    async def rip_library_song(self, url: LibrarySong, codec: str, flags: Flags = Flags()):
        music_token = await it(WebAPI).get_music_user_token()
        if not music_token:
            raise ValueError("无法读取 Music-User-Token，请先在「Apple ID 登录」页登录并确保内核内账号有效")
        catalog_id, title = await it(WebAPI).resolve_library_song(url.id, flags.language)
        song_ctx = DownloadPathContext(kind="library_song", container_name=title)
        song = Song(id=catalog_id, storefront=url.storefront, url="", type=URLType.Song)
        await self.rip_song(song, codec, flags, path_context=song_ctx)

    async def _rip_playlist_tracks(
        self,
        storefront: str,
        playlist_info: PlaylistInfo,
        codec: str,
        flags: Flags,
        playlist_kind: str = "playlist",
    ):
        playlist_info.songIdIndexMapping = {}
        logger = RipLogger(URLType.Playlist, playlist_info.data[0].id or "")
        curator = playlist_info.data[0].attributes.curatorName or "资料库"
        logger.set_fullname(curator, playlist_info.data[0].attributes.name)

        logger.create()

        async def on_children_done():
            logger.done()

        tracks = playlist_info.data[0].relationships.tracks.data or []
        resolved: list[tuple] = []

        for idx, track in enumerate(tracks):
            row = await it(WebAPI).resolve_catalog_track_entry(track, storefront)
            if not row:
                continue
            song_id, track_sf, _, _ = row
            playlist_info.songIdIndexMapping[song_id] = idx + 1
            resolved.append((track, song_id, track_sf))
        done_handler = ParentDoneHandler(len(resolved), on_children_done)
        playlist_ctx = DownloadPathContext(
            kind=playlist_kind,
            container_name=playlist_info.data[0].attributes.name or "Playlist",
        )

        for track, song_id, track_sf in resolved:
            song = Song(id=song_id, storefront=track_sf, url="", type=URLType.Song)
            safely_create_task(
                self.rip_song(
                    song, codec, flags, done_handler,
                    playlist=playlist_info, path_context=playlist_ctx,
                ),
            )

    @staticmethod
    def _decrypt_sample_retryable(exc: BaseException) -> bool:
        if isinstance(exc, asyncio.TimeoutError):
            return True
        msg = str(exc).lower()
        return any(
            marker in msg
            for marker in (
                "stream lost",
                "decryption failed",
                "decrypt stream",
                "unavailable",
                "eof",
                "tcp stream",
                "stream removed",
            )
        )

    @retry(
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception(_decrypt_sample_retryable),
    )
    async def decrypt_sample_with_retry(self, adam_id: str, key: str, sample: bytes, sample_index: int):
        task = self.download_manager.get_task(adam_id)
        if not task:
            raise Exception("Task cancelled or not found")

        # Reset future if it is already done (e.g. from previous failed attempt)
        if task.decrypted_samples_futures[sample_index].done():
            task.decrypted_samples_futures[sample_index] = asyncio.get_running_loop().create_future()

        future = task.decrypted_samples_futures[sample_index]

        # We need to send the command to wrapper manager
        await it(WrapperManager).decrypt(adam_id, key, sample, sample_index)

        # Wait for the future to be resolved by the callback
        return await asyncio.wait_for(future, timeout=240)

    async def on_decrypt_success(self, adam_id: str, key: str, sample: bytes, sample_index: int):
        it(Measurer).record_decrypt(len(sample))
        task = self.download_manager.get_task(adam_id)
        if task and sample_index in task.decrypted_samples_futures:
            if not task.decrypted_samples_futures[sample_index].done():
                task.decrypted_samples_futures[sample_index].set_result(sample)

    async def on_decrypt_failed(self, adam_id: str, key: str, sample: bytes, sample_index: int):
        task = self.download_manager.get_task(adam_id)
        if task and sample_index in task.decrypted_samples_futures:
            if not task.decrypted_samples_futures[sample_index].done():
                task.decrypted_samples_futures[sample_index].set_exception(Exception("Decryption failed callback"))

    async def on_decrypt_stream_lost(self):
        for task in self.download_manager.list_tasks():
            for fut in list(task.decrypted_samples_futures.values()):
                if not fut.done():
                    fut.set_exception(Exception("Decrypt stream lost"))

    # Removed recv_decrypted_sample and on_decrypt_done as they are replaced by linear flow in rip_song
