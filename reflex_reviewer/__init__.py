import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(filename)s:%(lineno)d - %(message)s"
    ),
    handlers=[logging.StreamHandler(sys.stdout)],
)


def review(*args, **kwargs):
    from .review import run

    return run(*args, **kwargs)


def distill(*args, **kwargs):
    from .distill import run

    return run(*args, **kwargs)


def refine(*args, **kwargs):
    from .refine import run

    return run(*args, **kwargs)


__version__ = "0.1.0"
__all__ = ["review", "distill", "refine"]
