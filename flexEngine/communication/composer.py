#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import time
import uuid
from flexEngine.security.security import *
def compose_command(cmd_type, params, priority=5, expiry_seconds=60):
    current_time = int(time.time())

    _command = {
        "id": str(uuid.uuid4()),
        "cmd": cmd_type,
        "priority": priority,                # Add command priority (e.g., 1=highest, 10=lowest)
        "expiry": current_time + expiry_seconds,  # Command expiry timestamp
        "params": params,
        "timestamp": current_time,
        "nonce": uuid.uuid4().hex
    }
    _command["signature"] = compute_signature(_command)
    return _command