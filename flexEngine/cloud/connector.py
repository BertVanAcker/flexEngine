#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import threading
import json
import redis

from flexEngine.security.security import verify_signature
from flexEngine.utils.constants import *
from flexEngine.communication.matrix import *
from flexEngine.command.commandProcessor import *
from flexEngine.logging.logger import *
class flexCloudConnector:
    def __init__(self, name="flexCloudConnector",primaryHardwareID="Oberon-5",projectID="sandbox",key="3fg445",  redis_host='localhost', redis_port=6379, redis_db=0,logger=None, verbose=False):
        """Initialize a device monitor component.

                Parameters
                ----------
                name : str, optional
                    The name of the component (default is "deviceMonitor").


                verbose : bool, optional
                    Enables verbose execution logging if set to True (default is False).

                Examples
                --------
                >> connector = flexCloudConnector(name="flexCloudConnector",verbose=False)

                """
        self._name = name
        self._verbose = verbose
        self.connectorID = primaryHardwareID+"@"+projectID+"-"+key          #devicename@projectname-key
        self._commandKey = self.connectorID+COMMUNICATIONMATRIX.COMMAND     #devicename@projectname-key:cmd

        # custom logger
        if logger is not None:
            self._logger = logger
        else:
            self._logger = Logger(name="customLogger", verbose=False)

        #---flexCloud setup---
        self.redis_client = redis.StrictRedis(host=redis_host, port=redis_port, db=redis_db, decode_responses=True)
        self.pubsub = self.redis_client.pubsub()
        self.pubsub.subscribe(**{
            self._commandKey: self._on_command
        })
        #---configure command processor---
        self._commandProcessor = commandProcessor(name="customCommandProcessor",logger=self._logger,verbose=verbose)

        # Start listener thread
        self.listener_thread = threading.Thread(target=self._listen, daemon=True)
        self.listener_thread.start()

    #---------------------------------flexCloud handlers--------------------------------------
    def _listen(self):
        # Listen to subscribed channels
        for message in self.pubsub.listen():
            if message['type'] == 'message':
                channel = message['channel']
                data = message['data']
                if self._verbose:print(f"Received message from {channel}: {data}")

    def _on_command(self, message):
        # Callback for 'command' channel
        if self._verbose:print(f"Command received.")
        try:
            command = json.loads(message["data"])
        except:
            command = GENERIC_STATUS.INCORRECT_CMD
        if self._verbose:print(command)

        # CHECK IF COMMAND IS VALID
        _isValid = verify_signature(command)
        if _isValid:
            self._logger.info("Command "+ command["id"]+" is valid.")
            # pass command to command processor
            self._commandProcessor.process_command(command)
        else:
            self._logger.error("Command " + command["id"] + " invalid, dropping the command.")

    def write(self, key, value):
        """
        Store a key-value pair in the data platform. If the value is a dictionary, it will be serialized.
        :param key: The key for storing the data
        :param value: The value to store (can be a dict, str, int, etc.)
        """
        #key = self.connectorID+":"+"data:"+key
        if isinstance(value, dict):
            value = json.dumps(value)  # Serialize the dictionary to a JSON string
        else:
            value = str(value)
        self.redis_client.set(key, value)
        # signal new data
        self.redis_client.publish(self.connectorID+"_update", "new data")

    def write_storage(self, key, value):
        """
        Store a key-value pair in the data platform. If the value is a dictionary, it will be serialized.
        :param key: The key for storing the data
        :param value: The value to store (can be a dict, str, int, etc.)
        """
        key = self.connectorID + ":" + "data:" + key
        if isinstance(value, dict):
            value = json.dumps(value)  # Serialize the dictionary to a JSON string
        self.redis_client.lpush(key,value)
        #signal new data
        self.redis_client.publish(self.connectorID+"_update", "new data")

    def read(self, key, queueSize = 1):
        """
        Retrieve a value from the data platform. If the value is JSON, it will be deserialized to a Python object.
        :param key: The key for retrieving the data
        :return: The value associated with the key or None if not found
        """
        value = self.redis_client.get(key)
        if value is not None:
            #value = value.decode('utf-8')  # Convert bytes to string       #TODO: CHECK IF WE CAN ALWAYS USE AUTO-DECODE OR WE NEED TO MANUALLY DECODE
            try:
                value = json.loads(value)  # Try to deserialize the value if it's a JSON string
            except json.JSONDecodeError:
                pass  # If it's not JSON, return it as a string
        return value

    def read_list(self, key, queueSize = 1, all=False):
        if all:
            list = self.redis_client.lrange(key, 0, -1)
        else:
            list = self.redis_client.lrange(key, 0, queueSize)

        return list



