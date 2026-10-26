"""仅记录异常事件到本地文件，用于运维排查（正常爬取/发信不写盘）。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from config import ABNORMAL_LOG_FILE, LOGS_DIR

_lock = threading.Lock()
_configured = False


def _ensure_configured() -> None:
    global _configured
    if _configured:
        return
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _configured = True


def log_abnormal(category: str, message: str, exc: BaseException | None = None) -> None:
    """
    追加一行异常日志。category 建议: crawl | email | push | server | outbox
    """
    _ensure_configured()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{category}] {message}"
    if exc is not None:
        line += f" | {type(exc).__name__}: {exc}"
    with _lock:
        with open(ABNORMAL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def get_abnormal_log_path() -> Path:
    _ensure_configured()
    return ABNORMAL_LOG_FILE


# 供 logging.Handler 可选挂载；当前以显式 log_abnormal 为主
class AbnormalOnlyFileHandler(logging.Handler):
    """将 WARNING 及以上写入 abnormal 日志（模块 logger 可选用）。"""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        try:
            msg = self.format(record)
            log_abnormal(record.name, msg)
        except Exception:
            self.handleError(record)
