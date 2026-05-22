"""
logger.py — централизованное логирование приложения.

Предоставляет singleton `log` (импортируй во всех модулях):
    from logger import log
    log.info("...")
    log.debug("...")   # только в файл
    log.warning("...")
    log.error("...")

Вывод:
  - Консоль (stdout): уровень INFO и выше
  - Файл arbi.log (рядом с этим файлом): все уровни включая DEBUG

Формат: "ЧЧ:ММ:СС  LEVEL    сообщение"
"""

import logging
import sys
from pathlib import Path

LOG_FILE = Path(__file__).parent / "arbi.log"

def setup_logger(name: str = "arbi") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S"
    )

    # Консоль — только INFO и выше
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Файл — всё, включая DEBUG
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = setup_logger()
