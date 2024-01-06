import re

from .utils import validator

mac_address_pattern = re.compile(r'^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')


@validator
def mac_address(value : str):
    """
    Return whether or not given value is a valid MAC address.

    If the value is valid MAC address this function returns ``True``,
    otherwise :class:`~validators.utils.ValidationFailure`.

    This validator is based on `WTForms MacAddress validator`_.

    .. _WTForms MacAddress validator:
       https://github.com/wtforms/wtforms/blob/master/wtforms/validators.py

    Examples::

        >>> mac_address('01:23:45:67:ab:CD')
        True

        >>> mac_address('00:00:00:00:00')
        ValidationFailure(func=mac_address, args={'value': '00:00:00:00:00'})

    .. versionadded:: 0.2

    :param value: Mac address string to validate
    """
    return mac_address_pattern.match(value).group(0) == value
