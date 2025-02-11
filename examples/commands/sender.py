#**************************************************************************
# * Copyright (C) 2023-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the hybridIO project.
# *
# * HybridIO can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import redis
from flexEngine.security.security import *
from flexEngine.utils.constants import *
from flexEngine.communication.composer import *
from flexEngine.communication.matrix import *


# Generate and store secret
secret = generate_shared_secret(32)
register_os_variable(secret, env_var_name=SECURITY_STORE.SHARED_SECRET)

# Prepare a command message as a JSON-encoded byte string.
cmd_type = COMMAND_TYPE.OTA_UPDATE
params = {"url": "https://github.com/BertVanAcker/rpio-examples.git"}
priority = 3
expiry_seconds = 120

# Call the function under test.
command = compose_command(cmd_type, params, priority, expiry_seconds)
command_data = json.dumps(command).encode('utf-8')

# Send command to flexEngine running on device (agent.py)
primaryHardwareID = "oberon5"
projectID = "examples"
key = "5g889g5"
_commandKey = primaryHardwareID+"@"+projectID+"-"+key+COMMUNICATIONMATRIX.COMMAND

# Publish the command message to Redis.
redis_client = redis.StrictRedis(host='localhost', port=6379, db=15, decode_responses=False)
redis_client.flushdb()
redis_client.publish(_commandKey, command_data)