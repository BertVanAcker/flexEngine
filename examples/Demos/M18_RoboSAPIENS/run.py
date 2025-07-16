# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import redis
from flexEngine.security.security import *
from flexEngine.utils.constants import *
from flexEngine.communication.composer import *
from flexEngine.communication.matrix import *
from flexEngine.utils.auxiliary import load_config

#fetch configuration of lattepandaD3_1
_config_lattepandaD3_1 = load_config("agents/lattepandaD3_1/configuration/config.yaml")
_device_config_lattepandaD3_1 = _config_lattepandaD3_1.get("deviceCredentials", {})

#fetch configuration of lattepandaD3_2
_config_lattepandaD3_2 = load_config("agents/lattepandaD3_2/configuration/config.yaml")
_device_config_lattepandaD3_2 = _config_lattepandaD3_2.get("deviceCredentials", {})

#fetch configuration of lattepandaD3_2
_config_lattepandaD3_3 = load_config("agents/lattepandaD3_3/configuration/config.yaml")
_device_config_lattepandaD3_3 = _config_lattepandaD3_3.get("deviceCredentials", {})


#fetch configuration
_config = load_config("configuration/config.yaml")
_flexCloud_config = _config.get("flexCloud", {})

# Generate and store secret
secret = _device_config_lattepandaD3_1.get("shared_secret","UNKNOWN")                       #TODO: discuss if SECRET THE SAME PER DEVICE OR NOT?
register_os_variable(secret, env_var_name=SECURITY_STORE.SHARED_SECRET)

# Prepare a command message as a JSON-encoded byte string.
cmd_type = COMMAND_TYPE.RUN
params = {"packageType": "flexia","app": "flexia-robosapiens-example","tag": ""}                #TODO: for now, I only look to flexIA.ini!
priority = 3
expiry_seconds = 120

# Call the function under test.
command = compose_command(cmd_type, params, priority, expiry_seconds)
command_data = json.dumps(command).encode('utf-8')


# Send command to flexEngine running on lattepandaD3_1
_commandKey = _device_config_lattepandaD3_1.get("primaryHardwareID", "UNKNOWN")+"@"+_device_config_lattepandaD3_1.get("projectID", "UNKNOWN")+"-"+_device_config_lattepandaD3_1.get("key", "UNKNOWN")+COMMUNICATIONMATRIX.COMMAND
redis_client = redis.StrictRedis(host=_flexCloud_config.get("redis_host", "localhost"), port=_flexCloud_config.get("redis_port", 6379), db=_flexCloud_config.get("redis_db", 15), decode_responses=False)
redis_client.publish(_commandKey, command_data)

# Send command to flexEngine running on lattepandaD3_1
_commandKey = _device_config_lattepandaD3_2.get("primaryHardwareID", "UNKNOWN")+"@"+_device_config_lattepandaD3_2.get("projectID", "UNKNOWN")+"-"+_device_config_lattepandaD3_2.get("key", "UNKNOWN")+COMMUNICATIONMATRIX.COMMAND
redis_client = redis.StrictRedis(host=_flexCloud_config.get("redis_host", "localhost"), port=_flexCloud_config.get("redis_port", 6379), db=_flexCloud_config.get("redis_db", 15), decode_responses=False)
redis_client.publish(_commandKey, command_data)

# Send command to flexEngine running on Lattepanda_3
_commandKey = _device_config_lattepandaD3_3.get("primaryHardwareID", "UNKNOWN")+"@"+_device_config_lattepandaD3_3.get("projectID", "UNKNOWN")+"-"+_device_config_lattepandaD3_3.get("key", "UNKNOWN")+COMMUNICATIONMATRIX.COMMAND
redis_client = redis.StrictRedis(host=_flexCloud_config.get("redis_host", "localhost"), port=_flexCloud_config.get("redis_port", 6379), db=_flexCloud_config.get("redis_db", 15), decode_responses=False)
redis_client.publish(_commandKey, command_data)


