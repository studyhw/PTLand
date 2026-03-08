#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
标准接口版“小 EC Tool”

功能（仅依赖 Windows + psutil 标准接口）：
- 周期性读取电池信息（电量、电源是否接入、估算剩余时间等）
- 同时输出到控制台与 CSV 日志文件，便于和充放电自动化流程联动分析

使用方式：
- 安装依赖：pip install psutil
- 运行：python "ec_tool.py"
- 打包为 exe 后，在 PTLand 客户端界面中，将 ECTool 路径指向该 exe
"""

import csv
import datetime as _dt
import logging
import os
import sys
import time
from typing import Optional, TextIO, Tuple

import psutil


LOG_DIR = "logs"
SAMPLE_INTERVAL_SECONDS = 10  # 采样间隔


def _get_base_dir() -> str:
    """
    返回程序运行的“基准目录”：
    - 以 .py 运行时：脚本所在目录
    - 以 PyInstaller 打包后的 .exe 运行时：exe 所在目录
    """
    if getattr(sys, "frozen", False):
        # PyInstaller onefile / onedir 模式下
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def setup_logging() -> None:
    base_dir = _get_base_dir()
    log_dir = os.path.join(base_dir, LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="[{asctime}] [{levelname}] {message}",
        style="{",
        datefmt="%H:%M:%S",
    )


def _open_csv() -> Tuple[csv.writer, TextIO]:
    """
    按日期创建 CSV 文件，文件名形如：
    ec_battery_2026-03-06_195500.csv
    """
    now = _dt.datetime.now()
    base_dir = _get_base_dir()
    log_dir = os.path.join(base_dir, LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)
    filename = f"ec_battery_{now.strftime('%Y-%m-%d_%H%M%S')}.csv"
    path = os.path.join(log_dir, filename)

    f: TextIO = open(path, "w", encoding="utf-8", newline="")
    writer: csv.writer = csv.writer(f)
    writer.writerow(
        [
            "timestamp",
            "percent",
            "secs_left",
            "power_plugged",
        ],
    )
    logging.info("开始记录电池数据到 CSV：%s", path)
    return writer, f


def _close_csv(f: Optional[TextIO]) -> None:
    if f is None:
        return
    try:
        f.close()
    except Exception:
        pass


def _format_secs(secs: int) -> str:
    if secs < 0:
        return "unknown"
    h = secs // 3600
    m = (secs % 3600) // 60
    return f"{h:02d}:{m:02d}"


def main() -> None:
    setup_logging()
    logging.info("EC Tool 启动（标准接口版，仅使用 psutil）")

    try:
        writer: Optional[csv.writer]
        csv_file: Optional[TextIO]
        writer, csv_file = _open_csv()
    except Exception as exc:  # noqa: BLE001
        logging.error("创建 CSV 日志失败：%s", exc)
        writer = None
        csv_file = None

    try:
        while True:
            try:
                batt = psutil.sensors_battery()
            except Exception as exc:  # noqa: BLE001
                logging.error("获取电池信息失败：%s", exc)
                batt = None

            if batt is None:
                logging.warning("无法获取电池信息（psutil.sensors_battery 返回 None）")
            else:
                percent = int(batt.percent)
                secs = batt.secsleft
                plugged = batt.power_plugged

                msg = (
                    f"电量：{percent}% | "
                    f"剩余时间：{_format_secs(secs)} | "
                    f"是否接电源：{'是' if plugged else '否'}"
                )
                logging.info(msg)

                if writer is not None:
                    try:
                        writer.writerow(
                            [
                                _dt.datetime.now().isoformat(timespec="seconds"),
                                percent,
                                secs,
                                int(bool(plugged)),
                            ],
                        )
                        if csv_file is not None:
                            csv_file.flush()
                            try:
                                os.fsync(csv_file.fileno())
                            except Exception:
                                # 某些平台可能不支持 fsync，忽略即可
                                pass
                    except Exception as exc:  # noqa: BLE001
                        logging.error("写入 CSV 失败：%s", exc)

            time.sleep(SAMPLE_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logging.info("收到中断信号，准备退出 EC Tool...")
    finally:
        _close_csv(csv_file)
        logging.info("EC Tool 已退出")


if __name__ == "__main__":
    # 避免当前目录是别的位置时找不到 logs 目录（例如从其他路径或打包 exe 启动）
    try:
        os.chdir(_get_base_dir())
    except Exception:
        pass
    main()

