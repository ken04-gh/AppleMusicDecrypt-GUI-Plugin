import time
from collections import deque
from typing import Type

from creart import CreateTargetInfo, AbstractCreator, exists_module


class Measurer:
    def __init__(self, sample_window=1, history_size=90):
        self._sample_window = sample_window
        self._download_records = deque()
        self._decrypt_records = deque()
        self._download_history = deque(maxlen=history_size)
        self._decrypt_history = deque(maxlen=history_size)
        self._running_tasks = 0

    def record_download(self, content_length: int):
        now = time.time()
        self._download_records.append((now, content_length))

    def record_decrypt(self, content_length: int):
        now = time.time()
        self._decrypt_records.append((now, content_length))

    def record_task_start(self):
        self._running_tasks += 1

    def record_task_finish(self):
        self._running_tasks -= 1

    def download_speed_bps(self) -> float:
        now = time.time()
        self._evict_old(self._download_records, now)
        return sum(x[1] for x in self._download_records) / self._sample_window

    def decrypt_speed_bps(self) -> float:
        now = time.time()
        self._evict_old(self._decrypt_records, now)
        return sum(x[1] for x in self._decrypt_records) / self._sample_window

    def record_speed_tick(self):
        self._download_history.append(self.download_speed_bps() / 1024)
        self._decrypt_history.append(self.decrypt_speed_bps() / 1024)

    def speed_history_kb_s(self) -> tuple[list[float], list[float]]:
        return list(self._download_history), list(self._decrypt_history)

    def download_speed(self) -> str:
        return self._format_speed(self.download_speed_bps())

    def decrypt_speed(self) -> str:
        return self._format_speed(self.decrypt_speed_bps())

    def _format_speed(self, speed_bps: float) -> str:
        speed_kb_s = speed_bps / 1024
        if speed_kb_s < 1024:
            return f"{speed_kb_s:.2f} kB/s"
        return f"{speed_kb_s / 1024:.2f} MB/s"

    def tasks_count(self):
        return self._running_tasks

    def _evict_old(self, dq, now):
        """只保留采样窗口内的数据"""
        while dq and now - dq[0][0] > self._sample_window:
            dq.popleft()




class MeasurerCreator(AbstractCreator):
    targets = (
        CreateTargetInfo("src.measurer", "Measurer"),
    )

    @staticmethod
    def available() -> bool:
        return exists_module("src.config")

    @staticmethod
    def create(create_type: Type[Measurer]) -> Measurer:
        return create_type()
