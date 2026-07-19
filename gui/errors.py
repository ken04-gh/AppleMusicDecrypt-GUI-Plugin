"""Format exceptions for GUI display with Chinese explanations."""

from __future__ import annotations

import re
from typing import Any, Callable, Optional


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def extract_message(exc: Any) -> str:
    if exc is None:
        return ""
    if isinstance(exc, str):
        return exc.strip()

    # Unwrap tenacity.RetryError → underlying ConnectError / HTTPError
    try:
        from tenacity import RetryError

        if isinstance(exc, RetryError) and exc.last_attempt is not None:
            inner = exc.last_attempt.exception()
            if inner is not None:
                return extract_message(inner)
    except Exception:
        pass

    msg = getattr(exc, "msg", None)
    if msg:
        return str(msg).strip()
    text = str(exc).strip()
    # RetryError[...] still stringified — try to surface inner type
    if text.startswith("RetryError") and "ConnectError" in text:
        return text
    return text


# English phrase (lower-case key) -> Chinese explanation
_PHRASE_ZH: dict[str, str] = {
    "no available instance": "解密实例繁忙或尚未登录。请先在「Apple ID 登录」页登录；若已登录可稍后重试或重新启动程序。",
    "解密服务尚未就绪": "管理服务可能已在线。请先完成 Apple ID 登录；登录后才会创建解密实例。",
    "管理服务尚未在线": "本地管理服务未就绪，请等待启动完成或结束 qemu 后重开。",
    "请先完成 apple id": "请先在「登录」页完成 Apple ID 登录，再查询/下载。",

    "尚未登录 apple id": "解密内核已就绪，但尚未登录 Apple ID。请到「Apple ID 登录」页完成登录后再查询或下载。",
    "no such account": "内核中找不到该 Apple ID，请确认邮箱输入正确或先登录。",
    "already login": "该 Apple ID 已在内核中登录，无需重复登录。",
    "failed to kill wrapper": "登出时无法停止内核内的解密进程，请稍后重试。",
    "login failed": "Apple ID 或密码错误，或账号状态异常。",
    "no active subscription": "该 Apple ID 未订阅 Apple Music，无法下载受 DRM 保护的曲目。",
    "connection refused": "无法连接本地解密服务（127.0.0.1:32767），请确认内核已启动。",
    "connecterror": "无法连接 Apple Music 网站。启动需访问 music.apple.com 获取 API 令牌（与登录无关）。请开启代理/VPN，在设置中填写代理后重试「连接 Apple API」。",
    "unexpected_eof": "与 music.apple.com 的 TLS 握手被中断（常见于网络屏蔽 Apple）。请使用可访问 Apple 的代理/VPN，并在设置中配置 HTTP 代理后重试。",
    "ssl:": "HTTPS/TLS 连接失败。请检查代理、系统时间与网络是否可访问 Apple 服务。",
    "retryerror": "网络请求多次重试仍失败。若出现在启动阶段，通常是无法访问 music.apple.com，请配置代理/VPN 后重试。",
    "music.apple.com": "无法访问 music.apple.com。程序启动时需从该站点获取开发者令牌。请配置代理或 VPN 后，在设置页点击「重试连接 Apple API」。",
    "unavailable": "解密服务暂时不可用，请稍后重试或重启程序。",
    "deadline exceeded": "操作超时，请检查网络或稍后重试。",
    "illegal url": "链接格式无效，请粘贴完整的 Apple Music 链接。",
    "unsupported urltype": "不支持的链接类型。",
    "missing dependency": "缺少必要的外部程序，请确认安装包 deps 目录完整。",
    "backend not started": "后台服务尚未启动完成，请等待启动结束后再操作。",
    "codec not found": "该曲目不提供所选编码格式，请换用其他编码或开启「编码回退」。",
    "lossless audio does not exist": "该曲目没有无损音源。",
    "integrity check": "下载文件完整性校验失败，文件可能已损坏。",
    "music-user-token": "无法读取资料库访问令牌，请先在「Apple ID 登录」页登录。",
    "music user token": "无法读取资料库访问令牌，请先在「Apple ID 登录」页登录。",
    "name 'vmaccountstate' is not defined": "程序内部错误：账号状态模块未正确加载（VmAccountState 未定义）。请更新到最新版本后重启。",
    "is not defined": "程序内部错误：组件未正确加载，请更新程序后重启。",
    "timed out": "连接或操作超时，内核启动约需 1–2 分钟，请耐心等待。",
    "timeout": "连接或操作超时，请稍后重试。",
    "file not found": "找不到所需文件，请确认安装目录完整。",
    "connectionerror": "网络连接失败，请检查代理设置或网络状态。",
    "httpx": "访问 Apple Music 接口失败，请检查网络或代理设置。",
    "grpc": "与本地解密服务通信失败，请重启程序。",
    "cancelled": "操作已取消。",
    "two_step": "需要两步验证码，请在弹窗中输入。",
    "catalog id": "无法解析资料库曲目对应的商店 ID，该曲可能已从商店下架。",
    "failed to get m3u8": "该曲目无法通过当前账号获取下载地址（可能未上架本地区、仅试听、或已从流媒体下架）。已快速跳过，无需长时间重试。",
    "conn read error": "与解密服务连接中断（内核内 wrapper 可能正在重启），请稍后重试。",
    "dial timeout": "连接解密实例超时，内核负载较高时请稍后再试。",
    "eof": "与解密服务通信意外结束，通常为瞬时错误，稍后重试即可。",
    "internal error from core": "解密样本时 gRPC 通信失败，常见于内核繁忙。请重启内核后重试；若仍失败请逐首下载。",
    "sendmessageoperation": "向解密内核发送音频数据失败，请重启内核后重试。",
    "does not exist in all available storefronts": "地区可用性检查失败（歌单含其他地区曲目时可能误报）。请更新程序后重试；若仍失败请用单曲链接下载。",
    "song not found on apple music": "Apple Music 目录中找不到该曲目，可能已下架或曲目 ID/地区解析有误。",
    "decryption failed": "解密样本失败，请重启内核后重试该曲。",
    "dial timeout": "解密内核连接超时（长时间批量下载后常见），请重启内核后继续下载剩余曲目。",
    "stream removed": "解密长连接被内核断开（常见于高码率 ALAC 或长时间批量下载）。程序会自动重连；若仍失败请重启内核后重试。",
    "tcp stream": "解密数据流意外结束，程序会自动重连；若反复出现请重启内核。",
    "stream lost": "解密流中断，程序正在自动重连；请稍候或重启内核后重试。",
    "decrypt stream": "解密通道未就绪或已断开，请等待自动恢复或重启内核。",
}


_REGEX_ZH: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (
        re.compile(r"name ['\"](\w+)['\"] is not defined", re.I),
        lambda m: f"程序内部错误：未定义组件「{m.group(1)}」，请更新程序后重启。",
    ),
    (
        re.compile(r"missing dependency:\s*(\S+)", re.I),
        lambda m: f"缺少依赖程序「{m.group(1)}」，请确认 deps 目录中存在该可执行文件。",
    ),
    (
        re.compile(r"illegal url", re.I),
        lambda _: "无法识别该链接，请使用 music.apple.com 的完整链接。",
    ),
    (
        re.compile(r"unsupported urltype[:\s-]*(\S+)", re.I),
        lambda m: f"不支持的链接类型：{m.group(1)}。",
    ),
    (
        re.compile(r"no such file or directory[:\s'\"]*([^'\"]+)?", re.I),
        lambda m: f"找不到文件：{m.group(1) or '未知路径'}。",
    ),
]


def translate_message(msg: str) -> Optional[str]:
    if not msg:
        return "发生未知错误，请查看日志或重启程序。"

    lower = msg.lower()
    for phrase, zh in _PHRASE_ZH.items():
        if phrase in lower:
            return zh

    for pattern, formatter in _REGEX_ZH:
        match = pattern.search(msg)
        if match:
            return formatter(match)

    if not _has_chinese(msg):
        return "程序返回了英文错误信息。请根据上方原文排查，或重启程序 / 重新登录 Apple ID 后重试。"
    return None


def format_error(exc: Any) -> str:
    """Return user-facing error text with a Chinese explanation appended when useful."""
    msg = extract_message(exc)
    if not msg:
        return "发生未知错误。\n\n【中文说明】请重启程序；若仍失败，请检查 deps 与内核是否正常。"

    zh = translate_message(msg)
    if zh is None:
        return msg
    if msg == zh:
        return msg
    if _has_chinese(msg):
        return f"{msg}\n\n【补充说明】{zh}"
    return f"{msg}\n\n【中文说明】{zh}"