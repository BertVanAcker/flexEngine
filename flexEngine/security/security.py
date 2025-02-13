#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import os
import uuid
import json
import hmac
import hashlib
import secrets
from flexEngine.utils.constants import *
def compute_signature(command):
    # Create a consistent string representation including the new fields
    SHARED_SECRET = os.environ.get(SECURITY_STORE.SHARED_SECRET)
    data_to_sign = (
        command["id"] +
        command["cmd"] +
        str(command["priority"]) +
        str(command["expiry"]) +
        json.dumps(command["params"], sort_keys=True) +
        str(command["timestamp"]) +
        command["nonce"]
    )
    return hmac.new(SHARED_SECRET.encode(), data_to_sign.encode(), hashlib.sha256).hexdigest()

def verify_signature(command):
    SHARED_SECRET = os.environ.get(SECURITY_STORE.SHARED_SECRET)
    expected = hmac.new(
        SHARED_SECRET.encode(),
        (
            command["id"] +
            command["cmd"] +
            str(command["priority"]) +
            str(command["expiry"]) +
            json.dumps(command["params"], sort_keys=True) +
            str(command["timestamp"]) +
            command["nonce"]
        ).encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, command["signature"])


def generate_shared_secret(length=32):
    """
    Generates a cryptographically secure random shared secret.

    Args:
        length (int): The number of bytes for the secret (default 32 bytes for 256 bits).

    Returns:
        bytes: The generated shared secret.
    """
    return secrets.token_bytes(length)

import os

def register_os_variable(secret, env_var_name=SECURITY_STORE.SHARED_SECRET):
    """
    Stores a given shared secret in an OS environment variable.

    Args:
        secret (str or bytes): The shared secret to store. If bytes, it will be converted to a hexadecimal string.
        env_var_name (str): The name of the environment variable to set (default is "SHARED_SECRET").

    Note:
        This only affects the current process (and its child processes). It does not persist across reboots
        or new sessions.
    """
    # If the secret is provided as bytes, convert it to a hex string for easier storage
    if isinstance(secret, bytes):
        secret = secret.hex()
    try:
        os.environ[env_var_name] = secret
        return True
    except:
        return False

def generate_app_tag(prefix="APP", unique_length=10):
    """
    Generates a unique tag for an application.

    The tag is composed of a prefix and a unique identifier generated
    from a UUID. The unique identifier is taken as the first `unique_length`
    characters of the hexadecimal representation of a UUID.

    Args:
        prefix (str): A string to prefix the tag (default is "APP").
        unique_length (int): Number of characters from the UUID to use (default is 8).

    Returns:
        str: The generated application tag.
    """
    # Generate a UUID and take the first `unique_length` characters of its hex representation
    unique_part = uuid.uuid4().hex[:unique_length]
    if prefix is not None:
        return f"{prefix}-{unique_part}"
    else:
        return unique_part