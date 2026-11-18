"""An internal wait policy cannot inject response fields or accept booleans."""

import pytest
from qualification_orders import application


@pytest.mark.parametrize("value", [True, "1\r\nx-secret: private-token"])
def test_invalid_wait_policy_is_refused_before_serving(value):
    with pytest.raises(ValueError, match="Bounded integer"):
        application({}, None, {}, "unused", lookup_retry_after_seconds=value)
