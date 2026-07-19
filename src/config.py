import tomllib
from typing import Type

from creart import exists_module
from creart.creator import AbstractCreator, CreateTargetInfo
from pydantic import BaseModel

CONFIG_VERSION = "0.0.10"


class Instance(BaseModel):
    url: str = "127.0.0.1:8080"
    secure: bool = False


class LocalInstance(BaseModel):
    enable: bool = False
    enableHardwareAcceleration: bool = False
    hardwareAccelerator: str = ""
    memorySize: str = "512M"
    cpuModel: str = "Cascadelake-Server-v5"
    showWindow: bool = False
    startArgs: str = "-host 0.0.0.0 -port 32767 -debug -mirror"


class Region(BaseModel):
    language: str = "zh-Hant-HK"
    languageNotExistWarning: bool = True


class Download(BaseModel):
    proxy: str = ""
    parallelNum: int = 1
    maxRunningTasks: int = 128
    appleCDNIP: str = ""
    codecAlternative: bool = True
    codecPriority: list[str] = ["alac", "ec3", "ac3", "aac"]
    atmosConventToM4a: bool = True
    failedSongNotPassIntegrityCheck: bool = False
    audioInfoFormat: str = ""
    songNameFormat: str = "{disk}-{tracknum:02d} {title}"
    dirPathFormat: str = "downloads"
    playlistDirPathFormat: str = "downloads"
    playlistSongNameFormat: str = "{playlistSongIndex:02d}. {artist} - {title}"
    saveLyrics: bool = True
    lyricsFormat: str = "lrc"
    lyricsExtra: list[str] = ["translation", "pronunciation"]
    saveCover: bool = True
    coverFormat: str = "jpg"
    coverSize: str = "5000x5000"
    maxSampleRate: int = 192000
    maxBitDepth: int = 24
    afterDownloaded: str = ""
    retryTime: int = 8
    maxWaitTime: int = 30


class Metadata(BaseModel):
    embedMetadata: list[str] = ["title", "artist", "album", "album_artist", "composer", "album_created",
                                "genre", "created", "track", "tracknum", "disk", "lyrics", "cover", "copyright",
                                "record_company", "upc", "isrc", "rtng"]


class Config(BaseModel):
    version: str = "0.0.0"
    region: Region
    instance: Instance
    localInstance: LocalInstance
    download: Download
    metadata: Metadata

    @classmethod
    def load_from_config(cls, config_file: str = "config.toml"):
        with open(config_file, "r", encoding="utf-8") as f:
            config = tomllib.loads(f.read())
        return cls.model_validate(config)

    def save_to_file(self, config_file: str = "config.toml"):
        def q(s: str) -> str:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

        def fmt_list(items: list[str]) -> str:
            return "[" + ", ".join(q(i) for i in items) + "]"

        lines = [
            "# DO NOT EDIT IT",
            f'version = {q(CONFIG_VERSION)}',
            "",
            "[instance]",
            f"url = {q(self.instance.url)}",
            f"secure = {'true' if self.instance.secure else 'false'}",
            "",
            "[localInstance]",
            f"enable = {'true' if self.localInstance.enable else 'false'}",
            f"enableHardwareAcceleration = {'true' if self.localInstance.enableHardwareAcceleration else 'false'}",
            f"hardwareAccelerator = {q(self.localInstance.hardwareAccelerator)}",
            f"memorySize = {q(self.localInstance.memorySize)}",
            f"cpuModel = {q(self.localInstance.cpuModel)}",
            f"showWindow = {'true' if self.localInstance.showWindow else 'false'}",
            f"startArgs = {q(self.localInstance.startArgs)}",
            "",
            "[region]",
            f"language = {q(self.region.language)}",
            f"languageNotExistWarning = {'true' if self.region.languageNotExistWarning else 'false'}",
            "",
            "[download]",
            f"proxy = {q(self.download.proxy)}",
            f"parallelNum = {self.download.parallelNum}",
            f"maxRunningTasks = {self.download.maxRunningTasks}",
            f"appleCDNIP = {q(self.download.appleCDNIP)}",
            f"codecAlternative = {'true' if self.download.codecAlternative else 'false'}",
            f"codecPriority = {fmt_list(self.download.codecPriority)}",
            f"atmosConventToM4a = {'true' if self.download.atmosConventToM4a else 'false'}",
            f"failedSongNotPassIntegrityCheck = {'true' if self.download.failedSongNotPassIntegrityCheck else 'false'}",
            f"audioInfoFormat = {q(self.download.audioInfoFormat)}",
            f"songNameFormat = {q(self.download.songNameFormat)}",
            f"dirPathFormat = {q(self.download.dirPathFormat)}",
            f"playlistDirPathFormat = {q(self.download.playlistDirPathFormat)}",
            f"playlistSongNameFormat = {q(self.download.playlistSongNameFormat)}",
            f"saveLyrics = {'true' if self.download.saveLyrics else 'false'}",
            f"lyricsFormat = {q(self.download.lyricsFormat)}",
            f"lyricsExtra = {fmt_list(self.download.lyricsExtra)}",
            f"saveCover = {'true' if self.download.saveCover else 'false'}",
            f"coverFormat = {q(self.download.coverFormat)}",
            f"coverSize = {q(self.download.coverSize)}",
            f"maxSampleRate = {self.download.maxSampleRate}",
            f"maxBitDepth = {self.download.maxBitDepth}",
            f"afterDownloaded = {q(self.download.afterDownloaded)}",
            f"retryTime = {self.download.retryTime}",
            f"maxWaitTime = {self.download.maxWaitTime}",
            "",
            "[metadata]",
            f"embedMetadata = {fmt_list(self.metadata.embedMetadata)}",
            "",
        ]
        with open(config_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def save_to_file(self, config_file: str = "config.toml"):
        def q(s: str) -> str:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

        def fmt_list(items: list[str]) -> str:
            return "[" + ", ".join(q(i) for i in items) + "]"

        lines = [
            "# DO NOT EDIT IT",
            f'version = {q(CONFIG_VERSION)}',
            "",
            "[instance]",
            f"url = {q(self.instance.url)}",
            f"secure = {'true' if self.instance.secure else 'false'}",
            "",
            "[localInstance]",
            f"enable = {'true' if self.localInstance.enable else 'false'}",
            f"enableHardwareAcceleration = {'true' if self.localInstance.enableHardwareAcceleration else 'false'}",
            f"hardwareAccelerator = {q(self.localInstance.hardwareAccelerator)}",
            f"memorySize = {q(self.localInstance.memorySize)}",
            f"cpuModel = {q(self.localInstance.cpuModel)}",
            f"showWindow = {'true' if self.localInstance.showWindow else 'false'}",
            f"startArgs = {q(self.localInstance.startArgs)}",
            "",
            "[region]",
            f"language = {q(self.region.language)}",
            f"languageNotExistWarning = {'true' if self.region.languageNotExistWarning else 'false'}",
            "",
            "[download]",
            f"proxy = {q(self.download.proxy)}",
            f"parallelNum = {self.download.parallelNum}",
            f"maxRunningTasks = {self.download.maxRunningTasks}",
            f"appleCDNIP = {q(self.download.appleCDNIP)}",
            f"codecAlternative = {'true' if self.download.codecAlternative else 'false'}",
            f"codecPriority = {fmt_list(self.download.codecPriority)}",
            f"atmosConventToM4a = {'true' if self.download.atmosConventToM4a else 'false'}",
            f"failedSongNotPassIntegrityCheck = {'true' if self.download.failedSongNotPassIntegrityCheck else 'false'}",
            f"audioInfoFormat = {q(self.download.audioInfoFormat)}",
            f"songNameFormat = {q(self.download.songNameFormat)}",
            f"dirPathFormat = {q(self.download.dirPathFormat)}",
            f"playlistDirPathFormat = {q(self.download.playlistDirPathFormat)}",
            f"playlistSongNameFormat = {q(self.download.playlistSongNameFormat)}",
            f"saveLyrics = {'true' if self.download.saveLyrics else 'false'}",
            f"lyricsFormat = {q(self.download.lyricsFormat)}",
            f"lyricsExtra = {fmt_list(self.download.lyricsExtra)}",
            f"saveCover = {'true' if self.download.saveCover else 'false'}",
            f"coverFormat = {q(self.download.coverFormat)}",
            f"coverSize = {q(self.download.coverSize)}",
            f"maxSampleRate = {self.download.maxSampleRate}",
            f"maxBitDepth = {self.download.maxBitDepth}",
            f"afterDownloaded = {q(self.download.afterDownloaded)}",
            f"retryTime = {self.download.retryTime}",
            f"maxWaitTime = {self.download.maxWaitTime}",
            "",
            "[metadata]",
            f"embedMetadata = {fmt_list(self.metadata.embedMetadata)}",
            "",
        ]
        with open(config_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))


class ConfigCreator(AbstractCreator):
    targets = (
        CreateTargetInfo("src.config", "Config"),
    )

    @staticmethod
    def available() -> bool:
        return exists_module("src.config")

    @staticmethod
    def create(create_type: Type[Config]) -> Config:
        return create_type.load_from_config()
