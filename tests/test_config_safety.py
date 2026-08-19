"""The paper-only gate must hard-fail on any violation."""
import pytest

from config import UnsafeConfigError
from tests.conftest import make_config


def test_ok_config_validates():
    make_config().validate()  # does not raise


def test_live_flag_is_rejected():
    with pytest.raises(UnsafeConfigError):
        make_config(i_understand_this_is_live=True).validate()


def test_non_paper_endpoint_is_rejected():
    from config import AlpacaCreds
    bad = make_config(alpaca=AlpacaCreds("PKx", "s", "https://api.alpaca.markets"))
    with pytest.raises(UnsafeConfigError):
        bad.validate()


def test_live_key_prefix_is_rejected():
    from config import AlpacaCreds, PAPER_ENDPOINT
    bad = make_config(alpaca=AlpacaCreds("AKLIVEKEY", "s", PAPER_ENDPOINT))
    with pytest.raises(UnsafeConfigError):
        bad.validate()
