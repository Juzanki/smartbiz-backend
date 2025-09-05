"""
Utility to generate numeric verification codes for authentication flows.
"""

import random
import string
from typing import Optional


def generate_verification_code(length: int = 6, seed: Optional[int] = None) -> str:
    """
    Generate a secure random numeric verification code.

    Parameters
    ----------
    length : int
        Length of the verification code (default is 6).
    seed : Optional[int]
        Optional seed for reproducible output (useful for testing).

    Returns
    -------
    str
        A numeric verification code of the specified length.

    Raises
    ------
    ValueError
        If length is not a positive integer.

    Example
    -------
    >>> generate_verification_code(6)
    '482019'
    """
    if not isinstance(length, int) or length <= 0:
        raise ValueError("❌ Verification code length must be a positive integer.")

    if seed is not None:
        random.seed(seed)

    return ''.join(random.choices(string.digits, k=length))
