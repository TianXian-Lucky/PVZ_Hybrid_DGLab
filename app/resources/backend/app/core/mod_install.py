"""游戏端 Mod 的检测与自动投放。

作者：TianXianbaobao
本插件为开源项目，请勿倒卖。禁止商用。

边界（必须遵守，否则会破坏用户环境）：

  * 只碰三个明确路径：Mods 目录、enabled_mods.json、本插件自带的 .pmod。
  * 绝不修改 mod_last_good.json —— 那是游戏的完整性校验记录，
    让它自己重建；擅自改写会触发回滚保护。
  * 合并 enabled_mods.json 时保留其中已有的其他 Mod ID。
  * 已有同名 .pmod 时只在 sha256 不同且用户允许时才替换。
  * 游戏正在运行时不做任何写入，只返回"需要重启"。
  * 任何失败都降级为提示，不抛异常、不中断插件。

本模块不导入任何第三方库，只用标准库与 subprocess。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

MOD_ID = "pvz-dghub-plant-damage"
MOD_FILENAME = "PVZHybrid-Watcher.pmod"
GAME_FOLDER = "植物大战僵尸杂交版"

# 游戏主程序进程名。Godot 导出的可执行文件与项目同名，
# 这里按前缀匹配，允许版本号后缀变化。
GAME_PROCESS_HINTS = ("植物大战僵尸杂交版", "PlantsVsZombies", "PVZHybrid")


def _appdata() -> Path:
    """按 Windows 惯例取漫游目录。测试可注入。"""
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def game_mods_dir(appdata: Path | None = None) -> Path:
    """Godot 按项目名生成用户数据目录，路径可完全推导，无需查找游戏安装位置。"""
    return (appdata or _appdata()) / "Godot" / "app_userdata" / GAME_FOLDER / "Mods"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class Plan:
    """一次安装计划。state 决定界面怎么提示。"""
    state: str                  # ok | ready | no_source | game_running | need_restart | error
    detail: str
    needs_restart: bool = False
    changed: bool = False


def game_running() -> bool:
    """检测游戏是否在运行。

    只用 tasklist（Windows 自带），不引入 psutil —— 插件对第三方依赖很克制。
    中文 Windows 的 tasklist 输出是本地代码页（GBK），不能按 UTF-8 解码，
    因此这里读 bytes 再按代码页解码；解不出来就退回忽略错误。
    非 Windows 或命令不可用时返回 False（宁可不拦，也不误拦）。
    """
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    raw = out.stdout or b""
    text = None
    for encoding in ("utf-8", "gbk", "mbcs", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    return any(hint in text for hint in GAME_PROCESS_HINTS)


class ConfigError(Exception):
    """enabled_mods.json 存在但读不出来 / 结构不对。

    这种情况必须停止写入 —— 盲目合并会覆盖掉用户的启用记录。
    """


def read_enabled(mods: Path) -> dict:
    """读 enabled_mods.json。

    文件不存在 => 返回空骨架（首次安装的正常情况）。
    文件存在但解析失败 => 抛 ConfigError，由调用方决定停止写入。
    """
    path = mods / "enabled_mods.json"
    if not path.exists():
        return {"schemaVersion": 2, "enabledIds": [], "preferredOrder": []}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"无法读取 enabled_mods.json：{type(exc).__name__}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError("enabled_mods.json 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError("enabled_mods.json 顶层不是对象")
    ids = data.get("enabledIds")
    if ids is not None and not isinstance(ids, list):
        raise ConfigError("enabledIds 不是数组")
    data["enabledIds"] = list(ids) if isinstance(ids, list) else []
    order = data.get("preferredOrder")
    data["preferredOrder"] = list(order) if isinstance(order, list) else []
    data.setdefault("schemaVersion", 2)
    return data


def is_enabled(mods: Path) -> bool:
    try:
        return MOD_ID in read_enabled(mods)["enabledIds"]
    except ConfigError:
        return False


def installed_by_id(mods: Path) -> Path | None:
    """按 Mod ID 在 Mods 目录里找已装的包，而不是只看固定文件名。

    用户或别的工具可能用别的文件名装过同一个 Mod（例如从别处下载的
    PVZHybridDGHub-PlantDamage.pmod）。只看文件名会重复装第二份，
    导致游戏里出现两个同 ID 的 Mod。
    """
    if not mods.is_dir():
        return None
    for candidate in sorted(mods.glob("*.pmod")):
        try:
            with zipfile.ZipFile(candidate) as archive:
                meta = json.loads(archive.read("mod.json").decode("utf-8-sig"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        if isinstance(meta, dict) and meta.get("id") == MOD_ID:
            return candidate
    return None


def inspect(source: Path | None, mods: Path | None = None) -> Plan:
    """只读检查，不写任何东西。用于启动检查展示。"""
    mods = mods or game_mods_dir()
    if source is None or not source.is_file():
        return Plan("no_source", "插件包内缺少游戏端 Mod 文件")
    if not mods.is_dir():
        return Plan("ready", "未找到游戏目录；请先在游戏里启用任意 Mod 以生成目录")
    try:
        enabled = MOD_ID in read_enabled(mods)["enabledIds"]
    except ConfigError as exc:
        return Plan("error", f"启用记录无法解析：{exc}")
    target = installed_by_id(mods) or (mods / MOD_FILENAME)
    if not target.is_file():
        return Plan("ready", "游戏端 Mod 尚未安装")
    if sha256(target) != sha256(source):
        return Plan("ready", f"已装的 Mod（{target.name}）版本与插件自带的不同")
    if not enabled:
        return Plan("ready", "Mod 已安装但未启用")
    return Plan("ok", "游戏端 Mod 已安装并启用")


def install(source: Path | None, mods: Path | None = None,
            allow_replace: bool = False) -> Plan:
    """把 .pmod 投放到 Mods 目录并登记启用。

    遇到游戏正在运行会直接返回 game_running，不写任何文件。
    启用记录读不出来时直接停止，避免覆盖用户原有的启用列表。
    """
    mods = mods or game_mods_dir()
    if source is None or not source.is_file():
        return Plan("no_source", "插件包内缺少游戏端 Mod 文件")
    if not mods.is_dir():
        return Plan("error", "未找到游戏目录；请先在游戏里启用任意 Mod 以生成目录")
    if game_running():
        return Plan("game_running", "检测到游戏正在运行，已跳过自动安装", needs_restart=True)

    # 先读启用记录：读不出来就什么都不写，绝不能用空骨架去覆盖。
    try:
        data = read_enabled(mods)
    except ConfigError as exc:
        return Plan("error", f"启用记录无法解析，已停止写入以免覆盖：{exc}")

    changed = False
    # 按 Mod ID 查重，而不是只看固定文件名 —— 否则同 ID 不同名会装出第二份。
    existing = installed_by_id(mods)
    target = existing or (mods / MOD_FILENAME)
    try:
        if existing is not None:
            same = sha256(existing) == sha256(source)
            if same:
                pass                                   # 已存在且内容一致，不动文件
            elif not allow_replace:
                return Plan("ready", f"已存在同 ID 的 Mod（{existing.name}），未覆盖")
            else:
                temp = existing.with_suffix(".pmod.tmp")
                temp.write_bytes(source.read_bytes())
                os.replace(temp, existing)
                changed = True
        elif target.is_file():
            # 同名文件但不是本 Mod（mod.json 里的 id 不符）：不碰它。
            return Plan("ready", f"{target.name} 已被其他 Mod 占用，未覆盖")
        else:
            temp = target.with_suffix(".pmod.tmp")
            temp.write_bytes(source.read_bytes())
            os.replace(temp, target)
            changed = True
    except OSError as exc:
        return Plan("error", f"写入 Mod 失败：{type(exc).__name__}")

    try:
        if MOD_ID not in data["enabledIds"]:
            data["enabledIds"].append(MOD_ID)
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            path = mods / "enabled_mods.json"
            temp = path.with_suffix(".json.tmp")
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, path)
            changed = True
    except OSError as exc:
        return Plan("error", f"写入启用记录失败：{type(exc).__name__}")

    if not changed:
        return Plan("ok", "游戏端 Mod 已安装并启用")
    return Plan("need_restart", "已安装游戏端 Mod，请重启游戏后生效", needs_restart=True, changed=True)
