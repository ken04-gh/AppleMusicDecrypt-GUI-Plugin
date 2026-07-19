# AppleMusicDecrypt GUI Plugin

适用于 Windows 的 AppleMusicDecrypt 图形界面增量插件。

本仓库只发布 GUI 插件，不是 AppleMusicDecrypt 的完整分发版。插件保留并复用上游的 `WrapperManager.decrypt()`、`Ripper` 和 `save()` 主链路，不能脱离完整的上游项目运行。

## 功能

- Apple Music 单曲、专辑、歌单和艺人链接的图形化任务管理
- 下载、解密、保存状态和实时吞吐显示
- Apple Music Windows 本地缓存扫描与导入
- 本地 wrapper-manager/QEMU 状态、登录及错误反馈
- 任务完成后可继续提交下一项任务
- 无控制台窗口的 GUI-only Windows EXE

## 运行要求

- Windows 10 或 Windows 11，x64
- 完整的 [WorldObservationLog/AppleMusicDecrypt](https://github.com/WorldObservationLog/AppleMusicDecrypt) v2 Windows 项目
- 上游项目已经具备 `.venv/`、`deps/`、`assets/wrapper-manager.qcow2` 和 `config.toml`

插件不提供 AppleMusicDecrypt 核心、第三方解密工具或 wrapper-manager 镜像。

## 安装

1. 从 GitHub Releases 下载 `AppleMusicDecrypt-GUI-Plugin-<版本>-windows-x64.zip`。
2. 校验 Release 中提供的 `SHA256SUMS.txt`。
3. 将 ZIP 内全部文件复制到完整的 AppleMusicDecrypt 项目根目录，允许覆盖同名文件。
4. 双击 `AppleMusicDecryptGUI.exe`。

目录正确时，EXE 旁应至少存在：

```text
AppleMusicDecrypt/
├─ AppleMusicDecryptGUI.exe
├─ main.py
├─ config.toml
├─ gui/
├─ src/
├─ deps/
└─ assets/
   └─ wrapper-manager.qcow2
```

如果缺少上游项目文件，GUI 会显示错误并退出。`start_gui.vbs` 和 `start_gui.bat` 仍可作为源码启动入口。

## GUI-only EXE

`AppleMusicDecryptGUI.exe` 约 32 MB，只封装 GUI、Python 运行时和 GUI 所需依赖。构建过程会分析依赖，但在最终归档中删除所有 `src` 和 `src.*` 核心模块；运行时从 EXE 所在的上游项目目录加载核心代码。

EXE 不包含：

- 上游完整 `src/`
- `deps/` 外部工具
- `assets/wrapper-manager.qcow2`
- `config.toml`
- 账号会话、下载记录、缓存或个人路径

## 从源码构建

先将本仓库覆盖到完整的上游 Windows 项目，然后在 PowerShell 中运行：

```powershell
.\build_gui.ps1
```

构建结果：

```text
dist/AppleMusicDecryptGUI.exe
```

构建配置见 `AppleMusicDecryptGUI.spec`。使用 `--verify-plugin` 可以无界面检查 GUI EXE 是否能够加载外部核心链路。

## 隐私

插件仓库和 Release 不包含本机登录状态。账号状态由用户自己的 wrapper-manager 虚拟机和原项目 `data/` 目录保存。发布前请勿加入 `config.toml`、使用过的 qcow2 镜像、`data/`、`downloads/` 或 `.venv/`。

## 法律提示

请仅处理你有权访问和使用的内容，并遵守所在地法律、Apple Media Services 条款及上游项目许可。本项目不提供内容、账号或订阅服务。

## 许可与致谢

本插件基于 AppleMusicDecrypt 的 AGPL-3.0 许可代码开发，同样以 GNU Affero General Public License v3.0 发布。详见 `LICENSE.txt` 和 `NOTICE.md`。

- 上游项目：[WorldObservationLog/AppleMusicDecrypt](https://github.com/WorldObservationLog/AppleMusicDecrypt)
- wrapper-manager：[WorldObservationLog/wrapper-manager](https://github.com/WorldObservationLog/wrapper-manager)
