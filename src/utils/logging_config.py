import logging
import sys


def configure(level: int = logging.INFO) -> None:
    stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=stream,
    )
