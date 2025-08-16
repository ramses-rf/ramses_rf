#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

This module wraps logger to provide bespoke functionality, especially for timestamps.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from datetime import datetime as dt
from logging.handlers import TimedRotatingFileHandler as _TimedRotatingFileHandler
from typing import Any

from .version import VERSION

DEV_MODE = False

_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)

try:
    import colorlog
except ModuleNotFoundError:
    _use_color_ = False
    _LOGGER.warning("Consider installing the colorlog library for colored logging")
else:
    _use_color_ = True
    # logging.basicConfig(format=DEFAULT_FMT, datefmt=DEFAULT_DATEFMT)  # Causes issues

DEFAULT_FMT = "%(asctime)s.%(msecs)03d %(message)s"
DEFAULT_DATEFMT = "%H:%M:%S"

# TODO: make account for the non-printing characters
CONSOLE_COLS = int(shutil.get_terminal_size(fallback=(int(2e3), 24)).columns - 1)

if DEV_MODE:  # Do this to have longer-format console messages
    # HH:MM:SS.sss vs YYYY-MM-DDTHH:MM:SS.ssssss, shorter format for the console
    CONSOLE_FMT = f"%(asctime)s%(frame).{CONSOLE_COLS - 27}s"
else:
    CONSOLE_FMT = f"%(asctime)s%(frame).{CONSOLE_COLS - 13}s"

PKT_LOG_FMT = "%(asctime)s%(frame)s"

BANDW_SUFFIX = "%(message)s%(error_text)s%(comment)s"
COLOR_SUFFIX = "%(yellow)s%(message)s%(red)s%(error_text)s%(cyan)s%(comment)s"

# How to strip ASCII colour from a text file:
#   sed -r "s/\x1B\[(([0-9]{1,2})?(;)?([0-9]{1,2})?)?[m,K,H,f,J]//g" file_name

LOG_COLOURS = {
    "DEBUG": "white",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "bold_red",
    "CRITICAL": "bold_red",
}  # default_log_colors


class _Logger(logging.Logger):  # use pkt.dtm for the log record timestamp
    """Logger instances represent a single logging channel."""

    def makeRecord(
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: Mapping[str, object] | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        """Create a specialized LogRecord with a bespoke timestamp.

        Will overwrite created and msecs (and thus asctime), but not relativeCreated.
        """

        extra = dict(extra or {})  # work with a copy
        extra["frame"] = extra.pop("_frame", "")
        if extra["frame"]:
            extra["frame"] = f" {extra['_rssi']} {extra['frame']}"

        rv = super().makeRecord(
            name, level, fn, lno, msg, args, exc_info, func, extra, sinfo
        )

        if hasattr(rv, "dtm"):  # if dtm := extra.get("dtm"):
            ct = rv.dtm.timestamp()
            rv.created = ct
            rv.msecs = (ct - int(ct)) * 1000

        if rv.msg:
            rv.msg = f" < {rv.msg}"

        if value := getattr(rv, "error_text", None):  # HACK
            rv.error_text = f" * {value}"

        if value := getattr(rv, "comment", None):  # HACK
            rv.comment = f" # {value}"

        return rv


class _Formatter:  # format asctime with configurable precision
    """Formatter instances convert a LogRecord to text."""

    converter = None  # was: time.localtime
    default_time_format = "%Y-%m-%dT%H:%M:%S.%f"
    precision = 6

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Return the creation time (asctime) of the LogRecord as formatted text.

        Allows for sub-millisecond precision, using datetime instead of time objects.
        """
        result = dt.fromtimestamp(record.created).strftime(
            datefmt or self.default_time_format
        )
        if "f" not in self.default_time_format:
            return result
        precision = self.precision or -1
        return result[: precision - 6] if -1 <= precision < 6 else result


class ColoredFormatter(_Formatter, colorlog.ColoredFormatter):  # type: ignore[misc]
    pass


class Formatter(_Formatter, logging.Formatter):  # type: ignore[misc]
    pass


class PktLogFilter(logging.Filter):  # record.levelno in (logging.INFO, logging.WARNING)
    """For packet log files, process only wanted packets."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True if the record is to be processed."""
        # if record._frame[4:] or record.comment or record.error_text:
        return record.levelno in (logging.INFO, logging.WARNING)


class StdErrFilter(logging.Filter):  # record.levelno >= logging.WARNING
    """For sys.stderr, process only wanted packets."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True if the record is to be processed."""  # WARNING-30, ERROR-40
        return record.levelno >= logging.WARNING


class StdOutFilter(logging.Filter):  # record.levelno < logging.WARNING
    """For sys.stdout, process only wanted packets."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True if the record is to be processed."""  # INFO-20, DEBUG-10
        return record.levelno < logging.WARNING


class TimedRotatingFileHandler(_TimedRotatingFileHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        assert self.when == "MIDNIGHT"
        self.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)

    # def emit(self, record):  # used only for debugging
    #     if True or self.shouldRollover(record):
    #         self.doRollover()
    #     return super().emit(record)

    def getFilesToDelete(self) -> list[str]:  # zxdavb: my version
        """Determine the files to delete when rolling over.

        Overridden as old log files not being deleted.
        """
        # See bpo-44753 (this code is as was before that commit), bpo45628, bpo-46063
        dirName, baseName = os.path.split(self.baseFilename)
        fileNames = os.listdir(dirName)
        result = []
        prefix = baseName + "."
        plen = len(prefix)
        for fileName in fileNames:
            if fileName[:plen] == prefix:
                suffix = fileName[plen:]
                if self.extMatch.match(suffix):
                    result.append(os.path.join(dirName, fileName))
        if len(result) < self.backupCount:
            result = []
        else:
            result.sort()
            result = result[: len(result) - self.backupCount]
        return result


def getLogger(  # permits a bespoke Logger class
    name: str | None = None, pkt_log: bool = False
) -> logging.Logger:
    """Return a logger with the specified name, creating it if necessary.

    Used to set record timestamps to its packet timestamp instead of the current time.
    """
    if name is None or not pkt_log:
        return logging.getLogger(name)

    # Acquire lock, so no-one else uses our Logger class
    try:  # TODO: remove this ASAP
        logging._acquireLock()  # type: ignore[attr-defined]
    except AttributeError:  # Python 3.13+
        logging._lock.acquire()  # type: ignore[attr-defined]

    klass = logging.getLoggerClass()
    logging.setLoggerClass(_Logger)

    logger = logging.getLogger(name)

    logging.setLoggerClass(klass)

    try:  # TODO: remove this ASAP
        logging._releaseLock()  # type: ignore[attr-defined]
    except AttributeError:  # Python 3.13+
        logging._lock.release()  # type: ignore[attr-defined]

    return logger


def set_logger_timesource(dtm_now: Callable[..., dt]) -> None:
    """Set a custom record factory, with a bespoke source of timestamps.

    Used to have records with the same datetime as the most recent packet log record.
    """

    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)

        ct = dtm_now().timestamp()
        record.created = ct
        record.msecs = (ct - int(ct)) * 1000

        return record

    old_factory = logging.getLogRecordFactory()

    logging.setLogRecordFactory(record_factory)


def set_pkt_logging(
    logger: logging.Logger,
    cc_console: bool = False,
    file_name: str | None = None,
    rotate_backups: int = 0,
    rotate_bytes: int | None = None,
) -> None:
    """Create/configure handlers, formatters, etc.

    Parameters:
    - file_name:      base of file to store packet logs in, from root
    - rotate_backups: keep this many copies, and rotate at midnight unless:
    - rotate_bytes:   rotate log files when log > rotate_size
    """

    logger.propagate = False  # log file is distinct from any app/debug logging
    logger.setLevel(logging.DEBUG)  # must be at least .INFO

    # as set_pkt_logging() may be called several times: to avoid duplicates in logs...
    for handler in logger.handlers:  # dont use logger.hasHandlers() as not propagating
        logger.removeHandler(handler)

    if file_name:  # note: this opens the packet_log file IO and may block
        if rotate_bytes:
            rotate_backups = rotate_backups or 2
            handler = logging.handlers.RotatingFileHandler(
                file_name, maxBytes=rotate_bytes, backupCount=rotate_backups
            )
        elif rotate_backups:
            handler = TimedRotatingFileHandler(
                file_name, when="MIDNIGHT", backupCount=rotate_backups
            )
        else:
            handler = logging.FileHandler(file_name)

        logfile_fmt = Formatter(fmt=PKT_LOG_FMT + BANDW_SUFFIX)

        handler.setFormatter(logfile_fmt)
        handler.setLevel(logging.INFO)  # .INFO (usually), or .DEBUG
        handler.addFilter(PktLogFilter())  # record.levelno in (.INFO, .WARNING)
        logger.addHandler(handler)

    elif cc_console:
        logger.addHandler(logging.NullHandler())

    else:
        logger.setLevel(logging.CRITICAL)
        return

    if cc_console:  # CC: output to stdout/stderr
        console_fmt: ColoredFormatter | Formatter
        if _use_color_:
            console_fmt = ColoredFormatter(
                fmt=f"%(log_color)s{CONSOLE_FMT + COLOR_SUFFIX}",
                reset=True,
                log_colors=LOG_COLOURS,
            )
        else:
            console_fmt = Formatter(fmt=CONSOLE_FMT + BANDW_SUFFIX)

        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(console_fmt)
        handler.setLevel(logging.WARNING)  # musr be .WARNING or less
        handler.addFilter(StdErrFilter())  # record.levelno >= .WARNING
        logger.addHandler(handler)

        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(console_fmt)
        handler.setLevel(logging.DEBUG)  # must be .INFO or less
        handler.addFilter(StdOutFilter())  # record.levelno < .WARNING
        logger.addHandler(handler)

    extras = {
        "_frame": "",
        "error_text": "",
        "comment": f"ramses_tx {VERSION}",
    }
    logger.warning("", extra=extras)  # initial log line
