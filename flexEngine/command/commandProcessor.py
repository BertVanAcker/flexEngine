#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
from flexEngine.utils.constants import *
from flexEngine.application.registry import *
import threading
class commandProcessor(object):
    def __init__(self,name="cmdProcessor",logger=None,verbose=False):
        """Initialize a Logger component.

                Parameters
                ----------
                name : string
                    name of the property components

                verbose : bool
                    component verbose execution

                See Also
                --------
                ..

                Examples
                --------
                >> cmdProcessor = commandProcessor(name="customProcessor",verbose=False)

                """

        # --- logger configuration ---
        self._name = name
        self._verbose = verbose
        self._logger = logger
        # Set to store received command IDs.
        self._received_command_ids = set()
        self._command_ids_lock = threading.Lock()

        #--- Registry manager ---
        self._registryManager = registryManager(name="registryManager",logger=self._logger,verbose=verbose)

        self._logger.info("Command processor initialized")


    def process_command(self,command):
        """Process a command """
        #TODO: ADD COMMAND QUEUE AND SORT BY PRIORITY
        try:
            #CHECK IF COMMAND IS ALREADY RECEIVED
            if not self.is_command_already_received(command):
                if command["cmd"] == COMMAND_TYPE.UNKNOWN:
                    if self._logger is not None: self._logger.error(COMMAND_TYPE.UNKNOWN+" command received. Please verify the command type")

                elif command["cmd"] == COMMAND_TYPE.OTA_UPDATE:
                    if self._logger is not None: self._logger.info(COMMAND_TYPE.OTA_UPDATE+" command received.")
                    if self._verbose:print(COMMAND_TYPE.OTA_UPDATE+" command received.")
                    #TODO: trigger OTA UPDATE
                elif command["cmd"] == COMMAND_TYPE.PING:
                    if self._logger is not None: self._logger.info(COMMAND_TYPE.PING+" command received.")
                    if self._verbose: print(COMMAND_TYPE.PING+" command received.")
                    #TODO: trigger PING RESPONSE
                elif command["cmd"] == COMMAND_TYPE.PUSH:
                    if self._logger is not None: self._logger.info(COMMAND_TYPE.PUSH + " command received.")
                    if self._verbose: print(COMMAND_TYPE.PUSH+" command received.")

                    self._registryManager.registerApplication(command=command)

        except Exception as e:
            if self._logger is not None: self._logger.error("Failed to process command.")


    def is_command_already_received(self,command):
        """
        Checks if a command has already been received by inspecting its unique 'id' field.

        This function uses a global set to keep track of command IDs that have already been processed.
        If the command has not been received before, its ID is added to the set.

        Args:
            command (dict): The command dictionary. Must include an 'id' key.

        Returns:
            bool: True if the command was already received, False if it is new.

        Raises:
            ValueError: If the command does not contain an 'id' field.
        """
        command_id = command.get("id")
        if command_id is None:
            raise ValueError("Command does not contain an 'id' field.")

        with self._command_ids_lock:
            if command_id in self._received_command_ids:
                return True
            else:
                self._received_command_ids.add(command_id)
                return False
