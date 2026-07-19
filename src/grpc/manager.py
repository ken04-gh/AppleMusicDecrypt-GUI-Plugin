import asyncio
import json
from typing import Awaitable, Callable, Optional, Type

from async_lru import alru_cache
from creart import AbstractCreator, CreateTargetInfo, exists_module, it
from grpc import ssl_channel_credentials
from grpc.aio import insecure_channel, Channel, secure_channel
from grpc.experimental import ChannelOptions
from grpc.aio import AioRpcError
from tenacity import retry_if_exception_type, retry, wait_random_exponential, stop_after_attempt, \
    retry_if_not_exception_message, before_sleep_log

from src.grpc.manager_pb2 import *
from src.grpc.manager_pb2_grpc import WrapperManagerServiceStub, google_dot_protobuf_dot_empty__pb2
from src.logger import GlobalLogger
from src.config import Config
from src.exceptions import LoginCancelledException, TwoFAResendException
from src.utils import safely_create_task


class WrapperManagerException(Exception):
    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)

    def __str__(self) -> str:
        return self.msg


_PERMANENT_M3U8_MARKERS = (
    "failed to get m3u8",
    "no such account",
    "no active subscription",
)
_TRANSIENT_M3U8_MARKERS = (
    "no available instance",
    "conn read",
    "eof",
    "dial timeout",
    "unavailable",
    "internal error",
    "i/o timeout",
)


def classify_m3u8_error(msg: str) -> str:
    lower = (msg or "").lower()
    if any(marker in lower for marker in _PERMANENT_M3U8_MARKERS):
        return "permanent"
    if any(marker in lower for marker in _TRANSIENT_M3U8_MARKERS):
        return "transient"
    return "unknown"


def is_permanent_m3u8_error(msg: str) -> bool:
    return classify_m3u8_error(msg) == "permanent"


class WrapperManager:
    _channel: Channel
    _stub: WrapperManagerServiceStub
    _decrypt_queue: asyncio.Queue[DecryptRequest]
    _login_lock: asyncio.Lock
    _GRPC_MAX_MESSAGE_BYTES = 64 * 1024 * 1024

    def __init__(self):
        self._login_lock = asyncio.Lock()
        self._decrypt_queue = asyncio.Queue()
        # Probe can be snappier; download stays conservative for stability
        self._m3u8_semaphore = asyncio.Semaphore(1)
        self._m3u8_min_interval_probe = 0.35
        self._m3u8_min_interval_download = 0.75
        self._m3u8_last_at = 0.0
        # Allow a small in-flight window so sample decrypt pipelines can feed the stream
        self._decrypt_semaphore = asyncio.Semaphore(8)
        self._channel_url = ""
        self._channel_secure = False
        self._decrypt_stream_task: Optional[asyncio.Task] = None
        self._decrypt_keepalive_task: Optional[asyncio.Task] = None
        self._decrypt_stream_ready = asyncio.Event()
        self._decrypt_stopping = False
        self._on_decrypt_success: Optional[Callable] = None
        self._on_decrypt_failure: Optional[Callable] = None
        self._on_stream_lost: Optional[Callable[[], Awaitable[None]]] = None

    async def close_channel(self):
        if getattr(self, "_channel", None):
            try:
                await self._channel.close()
            except Exception:
                pass

    async def reconnect_channel(self):
        if not self._channel_url:
            return
        self.status.cache_invalidate()
        await self.close_channel()
        await self.init(self._channel_url, self._channel_secure)

    async def init(self, url: str, secure: bool):
        self._channel_url = url
        self._channel_secure = secure
        service_config_json = json.dumps(
            {
                "methodConfig": [
                    {
                        "name": [{}],
                        "retryPolicy": {
                            "maxAttempts": 5,
                            "initialBackoff": "0.1s",
                            "maxBackoff": "1s",
                            "backoffMultiplier": 2,
                            "retryableStatusCodes": ["UNAVAILABLE", "INTERNAL"],
                        },
                    }
                ]
            }
        )
        max_msg = self._GRPC_MAX_MESSAGE_BYTES
        options = (
            (ChannelOptions.SingleThreadedUnaryStream, 1),
            ("grpc.service_config", service_config_json),
            ("grpc.max_send_message_length", max_msg),
            ("grpc.max_receive_message_length", max_msg),
            ("grpc.keepalive_time_ms", 30000),
            ("grpc.keepalive_timeout_ms", 10000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.min_ping_interval_without_data_ms", 10000),
        )
        if secure:
            self._channel = secure_channel(url, credentials=ssl_channel_credentials(), options=options)
        else:
            self._channel = insecure_channel(url, options=options)
        self._stub = WrapperManagerServiceStub(self._channel)
        return self

    @alru_cache
    async def status(self) -> StatusData:
        resp: StatusReply = await self._stub.Status(google_dot_protobuf_dot_empty__pb2.Empty)
        if resp.header.code != 0:
            raise WrapperManagerException(resp.header.msg)
        return resp.data

    async def login(self, username: str, password: str, on_2fa: Callable[[str, str], Awaitable[str]]):
        await self._login_lock.acquire()

        login_queue = asyncio.Queue()

        async def request_stream():
            while True:
                item = await login_queue.get()
                if item is None:
                    break
                yield item

        stream = self._stub.Login(request_stream())

        await login_queue.put(LoginRequest(data=LoginData(username=username, password=password)))

        async for reply in stream:
            reply: LoginReply
            match reply.header.code:
                case -1:
                    self._login_lock.release()
                    await login_queue.put(None)
                    raise WrapperManagerException(reply.header.msg)
                case 0:
                    self._login_lock.release()
                    await login_queue.put(None)
                    return
                case 2:
                    try:
                        two_step_code = await on_2fa(username, password)
                    except (LoginCancelledException, TwoFAResendException):
                        self._login_lock.release()
                        await login_queue.put(None)
                        raise
                    await login_queue.put(LoginRequest(data=LoginData(
                        username=username,
                        password=password,
                        two_step_code=two_step_code)))

    async def decrypt(self, adam_id: str, key: str, sample: bytes, sample_index: int):
        # Keep a modest concurrency so the VM queue stays full without flooding gRPC.
        async with self._decrypt_semaphore:
            await self._wait_decrypt_stream_ready()
            await self._decrypt_queue.put(
                DecryptRequest(data=DecryptData(adam_id=adam_id, key=key, sample_index=sample_index,
                                                sample=sample)))

    async def _bridge_decrypt_queue(self, bridge: asyncio.Queue):
        try:
            while not self._decrypt_stopping:
                item = await self._decrypt_queue.get()
                await bridge.put(item)
                if item is None:
                    break
        except asyncio.CancelledError:
            pass

    async def _wait_decrypt_stream_ready(self, timeout: float = 120.0):
        if self._decrypt_stopping:
            raise WrapperManagerException("decrypt stream stopping")
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._decrypt_stream_ready.is_set():
                return
            if self._decrypt_stream_task and self._decrypt_stream_task.done() and not self._decrypt_stopping:
                self._decrypt_stream_task = safely_create_task(self._decrypt_stream_loop())
            await asyncio.sleep(0.2)
        raise WrapperManagerException("decrypt stream not ready")

    async def wait_decrypt_stream_ready(self, timeout: float = 120.0):
        await self._wait_decrypt_stream_ready(timeout)

    async def stop_decrypt_stream(self):
        self._decrypt_stopping = True
        self._decrypt_stream_ready.clear()
        if self._decrypt_keepalive_task and not self._decrypt_keepalive_task.done():
            self._decrypt_keepalive_task.cancel()
            self._decrypt_keepalive_task = None
        if self._decrypt_stream_task and not self._decrypt_stream_task.done():
            self._decrypt_stream_task.cancel()
            try:
                await self._decrypt_stream_task
            except asyncio.CancelledError:
                pass
        self._decrypt_stream_task = None

    async def decrypt_init(
        self,
        on_success: Callable[[str, str, bytes, int], Awaitable[None]],
        on_failure: Callable[[str, str, bytes, int], Awaitable[None]],
        on_stream_lost: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        await self.stop_decrypt_stream()
        self._decrypt_stopping = False
        self._on_decrypt_success = on_success
        self._on_decrypt_failure = on_failure
        self._on_stream_lost = on_stream_lost
        self._decrypt_stream_ready.clear()
        self._decrypt_stream_task = safely_create_task(self._decrypt_stream_loop())

    async def _handle_decrypt_reply(self, reply: DecryptReply):
        if reply.data.adam_id == "KEEPALIVE":
            return
        match reply.header.code:
            case -1:
                safely_create_task(
                    self._on_decrypt_failure(
                        reply.data.adam_id, reply.data.key, reply.data.sample, reply.data.sample_index,
                    ))
            case 0:
                safely_create_task(
                    self._on_decrypt_success(
                        reply.data.adam_id, reply.data.key, reply.data.sample, reply.data.sample_index,
                    ))

    async def _decrypt_stream_loop(self):
        backoff = 1.0
        while not self._decrypt_stopping:
            bridge: asyncio.Queue = asyncio.Queue()
            bridge_task = asyncio.create_task(self._bridge_decrypt_queue(bridge))
            try:
                async def request_stream():
                    while not self._decrypt_stopping:
                        item = await bridge.get()
                        if item is None:
                            return
                        yield item

                stream = self._stub.Decrypt(request_stream())
                self._decrypt_stream_ready.set()
                backoff = 1.0
                if not self._decrypt_keepalive_task or self._decrypt_keepalive_task.done():
                    self._decrypt_keepalive_task = safely_create_task(self._decrypt_keepalive())
                async for reply in stream:
                    await self._handle_decrypt_reply(reply)
                raise WrapperManagerException("decrypt stream closed")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._decrypt_stream_ready.clear()
                if self._decrypt_keepalive_task and not self._decrypt_keepalive_task.done():
                    self._decrypt_keepalive_task.cancel()
                    self._decrypt_keepalive_task = None
                if self._on_stream_lost:
                    try:
                        await self._on_stream_lost()
                    except Exception:
                        pass
                if self._decrypt_stopping:
                    break
                it(GlobalLogger).logger.warning(f"Decrypt stream lost ({exc}), reconnecting...")
                try:
                    await self.reconnect_channel()
                except Exception as reconnect_exc:
                    it(GlobalLogger).logger.warning("Decrypt channel reconnect failed: %s", reconnect_exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 20.0)
            finally:
                bridge_task.cancel()
                try:
                    await bridge_task
                except asyncio.CancelledError:
                    pass
                try:
                    bridge.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def _decrypt_keepalive(self):
        while not self._decrypt_stopping and self._decrypt_stream_ready.is_set():
            await self._decrypt_queue.put(DecryptRequest(data=DecryptData(adam_id="KEEPALIVE")))
            await asyncio.sleep(15)

    async def _throttle_m3u8(self, *, probe: bool = False):
        loop = asyncio.get_running_loop()
        interval = self._m3u8_min_interval_probe if probe else self._m3u8_min_interval_download
        elapsed = loop.time() - self._m3u8_last_at
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)

    async def _m3u8_once(self, adam_id: str) -> str:
        resp: M3U8Reply = await self._stub.M3U8(M3U8Request(data=M3U8DataRequest(adam_id=adam_id)))
        if resp.header.code != 0:
            raise WrapperManagerException(resp.header.msg)
        return resp.data.m3u8

    async def _m3u8_request(self, adam_id: str, *, max_attempts: int = 8, probe: bool = False) -> str:
        last_exc: Optional[WrapperManagerException] = None
        for attempt in range(max_attempts):
            try:
                return await self._m3u8_once(adam_id)
            except WrapperManagerException as exc:
                last_exc = exc
                kind = classify_m3u8_error(exc.msg)
                if kind == "permanent":
                    raise
                if attempt + 1 >= max_attempts:
                    raise
                if kind == "transient":
                    wait_sec = min(0.8 * (2 ** attempt), 6.0 if probe else 16.0)
                else:
                    wait_sec = min(0.6 * (attempt + 1), 2.5 if probe else 6.0)
                it(GlobalLogger).logger.warning(
                    f"Retrying m3u8 for adamId {adam_id} in {wait_sec:.1f}s "
                    f"({attempt + 1}/{max_attempts}): {exc.msg}",
                )
                await asyncio.sleep(wait_sec)
        if last_exc:
            raise last_exc
        raise WrapperManagerException(f"failed to get m3u8 of adamId: {adam_id}")

    async def m3u8(self, adam_id: str, *, probe: bool = False) -> str:
        async with self._m3u8_semaphore:
            await self._throttle_m3u8(probe=probe)
            try:
                if probe:
                    # Quality probe: fewer retries, shorter waits
                    return await self._m3u8_request(adam_id, max_attempts=2, probe=True)
                return await self._m3u8_request(adam_id, max_attempts=8, probe=False)
            finally:
                self._m3u8_last_at = asyncio.get_running_loop().time()

    @retry(retry=((retry_if_exception_type(WrapperManagerException)) & (
            retry_if_not_exception_message('no such account'))),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime), before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def logout(self, username: str):
        resp: LogoutReply = await self._stub.Logout(LogoutRequest(data=LogoutData(username=username)))
        if resp.header.code != 0:
            raise WrapperManagerException(resp.header.msg)
        return

    @retry(retry=((retry_if_exception_type(WrapperManagerException)) & (
            retry_if_not_exception_message('no available instance'))),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime), before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def lyrics(self, adam_id: str, language: str, region: str) -> str:
        resp: LyricsReply = await self._stub.Lyrics(LyricsRequest(
            data=LyricsDataRequest(adam_id=adam_id, language=language, region=region)))
        if resp.header.code != 0:
            raise WrapperManagerException(resp.header.msg)
        return resp.data.lyrics

    @retry(retry=((retry_if_exception_type(WrapperManagerException)) & (
            retry_if_not_exception_message('no available instance'))),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime), before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def webPlayback(self, adam_id: str) -> str:
        resp: WebPlaybackReply = await self._stub.WebPlayback(WebPlaybackRequest(
            data=WebPlaybackDataRequest(adam_id=adam_id)
        ))
        if resp.header.code != 0:
            raise WrapperManagerException(resp.header.msg)
        return resp.data.m3u8

    @retry(retry=((retry_if_exception_type(WrapperManagerException)) & (
            retry_if_not_exception_message('no available instance'))),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(it(Config).download.retryTime), before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def license(self, adam_id: str, challenge: str, kid: str) -> str:
        resp: LicenseReply = await self._stub.License(LicenseRequest(
            data=LicenseDataRequest(adam_id=adam_id, challenge=challenge, uri=kid)
        ))
        if resp.header.code != 0:
            raise WrapperManagerException(resp.header.msg)
        return resp.data.license


class WMCreator(AbstractCreator):
    targets = (
        CreateTargetInfo("src.grpc.manager", "WrapperManager"),
    )

    @staticmethod
    def available() -> bool:
        return exists_module("src.grpc.manager")

    @staticmethod
    def create(create_type: Type[WrapperManager]) -> WrapperManager:
        return create_type()
