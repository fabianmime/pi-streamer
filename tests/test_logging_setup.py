import logging

import pytest

from app import configure_logging


@pytest.mark.parametrize("level_name,expected", [
    ("DEBUG", logging.DEBUG),
    ("INFO", logging.INFO),
    ("WARNING", logging.WARNING),
    ("ERROR", logging.ERROR),
])
def test_configure_logging_sets_root_level(level_name, expected):
    configure_logging(level_name)
    assert logging.getLogger().level == expected


def test_configure_logging_falls_back_to_info_for_unknown_level():
    configure_logging("NOT_A_REAL_LEVEL")
    assert logging.getLogger().level == logging.INFO
