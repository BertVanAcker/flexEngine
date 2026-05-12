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


def push_appliction(type="github",url = "https://github.com/BertVanAcker/flexia-robosapiens-example.git",branche="main",version="v0.0.1",devices=None,flexCloud_config=None):
    """ Mock-up of the flexCloud API for pushing applications to devices"""

    # ------------- Format command -------------
    cmd_type = COMMAND_TYPE.PUSH
    params = {"type": type, "url": url,"version":version,"branch": branche}
    priority = 3
    expiry_seconds = 120

    # Call the function under test.
    command = compose_command(cmd_type, params, priority, expiry_seconds)
    command_data = json.dumps(command).encode('utf-8')

    # ------------- Send command to devices ------------- #NOTE: this push is orchatrated by flexCloud in real implementation
    for device in devices:
          # devicename@projectname-key:cmd
        #_commandKey = device.get("primaryHardwareID","UNKNOWN") + "@" + device.get("projectID","UNKNOWN") + "-" + device.get("key", "UNKNOWN") + COMMUNICATIONMATRIX.COMMAND        #TODO: this needs to be updated!
        _commandKey = device.get("projectID","UNKNOWN") + "-" + device.get("key", "UNKNOWN") +COMMUNICATIONMATRIX.DEVICES+ device.get("primaryHardwareID", "UNKNOWN") +  COMMUNICATIONMATRIX.COMMAND
        redis_client = redis.StrictRedis(host=flexCloud_config.get("redis_host", "localhost"),port=flexCloud_config.get("redis_port", 6379),db=flexCloud_config.get("redis_db", 15), decode_responses=False)
        redis_client.publish(_commandKey, command_data)

def run_appliction(packageType="flexia",applicationName = "flexia-robosapiens-example",tag="",devices=None,flexCloud_config=None):
    """ Mock-up of the flexCloud API for running applications to devices"""

    # ------------- Format command -------------
    # Prepare a command message as a JSON-encoded byte string.
    cmd_type = COMMAND_TYPE.RUN
    params = {"packageType": packageType, "app": applicationName,"tag": tag}
    priority = 3
    expiry_seconds = 120

    # Call the function under test.
    command = compose_command(cmd_type, params, priority, expiry_seconds)
    command_data = json.dumps(command).encode('utf-8')

    # ------------- Send command to devices ------------- #NOTE: this push is orchatrated by flexCloud in real implementation
    for device in devices:
        #_commandKey = device.get("primaryHardwareID","UNKNOWN") + "@" + device.get("projectID","UNKNOWN") + "-" + device.get("key", "UNKNOWN") + COMMUNICATIONMATRIX.COMMAND
        _commandKey = device.get("projectID", "UNKNOWN") + "-" + device.get("key","UNKNOWN") + COMMUNICATIONMATRIX.DEVICES + device.get("primaryHardwareID", "UNKNOWN") + COMMUNICATIONMATRIX.COMMAND
        redis_client = redis.StrictRedis(host=flexCloud_config.get("redis_host", "localhost"),port=flexCloud_config.get("redis_port", 6379),db=flexCloud_config.get("redis_db", 15), decode_responses=False)
        redis_client.publish(_commandKey, command_data)

def stop_appliction(packageType="flexia",applicationName = "flexia-robosapiens-example",components="-",devices=None,flexCloud_config=None):
    """ Mock-up of the flexCloud API for running applications to devices"""

    # ------------- Format command -------------
    # Prepare a command message as a JSON-encoded byte string.
    cmd_type = COMMAND_TYPE.STOP
    params = {"packageType": packageType, "app": applicationName,"components": components}  # components -> list of components to stop, ; seperated
    priority = 3
    expiry_seconds = 120

    # Call the function under test.
    command = compose_command(cmd_type, params, priority, expiry_seconds)
    command_data = json.dumps(command).encode('utf-8')

    # ------------- Send command to devices ------------- #NOTE: this push is orchatrated by flexCloud in real implementation
    for device in devices:
        #_commandKey = device.get("primaryHardwareID","UNKNOWN") + "@" + device.get("projectID","UNKNOWN") + "-" + device.get("key", "UNKNOWN") + COMMUNICATIONMATRIX.COMMAND
        _commandKey = device.get("projectID", "UNKNOWN") + "-" + device.get("key","UNKNOWN") + COMMUNICATIONMATRIX.DEVICES + device.get("primaryHardwareID", "UNKNOWN") + COMMUNICATIONMATRIX.COMMAND
        redis_client = redis.StrictRedis(host=flexCloud_config.get("redis_host", "localhost"),port=flexCloud_config.get("redis_port", 6379),db=flexCloud_config.get("redis_db", 15), decode_responses=False)
        redis_client.publish(_commandKey, command_data)