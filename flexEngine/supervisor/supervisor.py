#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import yaml
import logging

from flexEngine.application.manager import applicationManager
from flexEngine.application.registry import registryManager
from flexEngine.cloud.connector import flexCloudConnector
from flexEngine.command.commandProcessor import commandProcessor
from flexEngine.device.heartbeat import heartbeat
from flexEngine.device.monitor import monitor
from flexEngine.security.security import register_os_variable
from flexEngine.utils.auxiliary import load_config
from flexEngine.utils.constants import SECURITY_STORE


class supervisor(object):
    def __init__(self,config='config.yml',verbose=False):
        """Initialize the supervisor component.

        Parameters
        ----------
        config : str, optional
            Path to the configuration file.

        Examples
        --------
        >> _supervisor = supervisor()

        """

        self._config = load_config(config)
        self._device_config = self._config.get("deviceCredentials", {})
        self._flexCloud_config = self._config.get("flexCloud", {})
        self._heartbeat_config = self._config.get("heartbeat", {})
        #--- Supervisor logger ---
        self._logger = self._initialize_logger()
        #--- flexCloud connector ---
        self._flexCloudConnector = flexCloudConnector(
            name=self._device_config.get("name", "UNKNOWN"),
            primaryHardwareID=self._device_config.get("primaryHardwareID", "UNKNOWN"),
            projectID=self._device_config.get("projectID", "UNKNOWN"),
            key=self._device_config.get("key", "UNKNOWN"),
            redis_host=self._flexCloud_config.get("redis_host", "localhost"),
            redis_port=self._flexCloud_config.get("redis_port", 6379),
            redis_db=self._flexCloud_config.get("redis_db", 0),
            logger=self._logger,
            verbose=verbose
        )
        #--- flexCloud heartbeat ---
        self._heartbeat = heartbeat(
            redis_client=self._flexCloudConnector.redis_client,                 #TODO: check if this works or we need to use the cloud connector interface
            primaryHardwareID=self._device_config.get("primaryHardwareID", "UNKNOWN"),
            projectID=self._device_config.get("projectID", "UNKNOWN"),
            key=self._device_config.get("key", "UNKNOWN"),
            heartbeat_interval=self._heartbeat_config.get("heartbeat_interval", 10),
            ttl=self._heartbeat_config.get("ttl", 12),
            logger=self._logger)

        # --- Registry manager ---
        self._registryManager = registryManager(name="registryManager",flexCloudConnector=self._flexCloudConnector, logger=self._logger, verbose=verbose)

        # --- Application manager ---
        self._applicationManager = applicationManager(name="applicationManager",flexCloudConnector=self._flexCloudConnector, logger=self._logger, verbose=verbose)

        # ---configure command processor---
        self._commandProcessor = commandProcessor(name="customCommandProcessor", logger=self._logger,registryManager=self._registryManager,applicationManager=self._applicationManager, verbose=verbose)
        self._flexCloudConnector.commandProcessor = self._commandProcessor

        #--- Device monitor ---
        self._deviceMonitor = monitor(name="testMonitor",flexCloudConnector=self._flexCloudConnector,verbose=verbose)

        #--- Register shared_secret ---
        self._isRegistered = register_os_variable(self._device_config.get("shared_secret","UNKNOWN"), env_var_name=SECURITY_STORE.SHARED_SECRET)
        if self._isRegistered:
            self._logger.info("Stored shared secret in environment variable "+ SECURITY_STORE.SHARED_SECRET)
        else:
            self._logger.error("Failed to store shared secret in environment variable " + SECURITY_STORE.SHARED_SECRET)


    def _initialize_logger(self):
        """Initialize the logger (same as before)."""
        log_config = self._config.get("logging", {})
        logger = logging.getLogger(self.__class__.__name__)
        log_level = log_config.get("level", "INFO").upper()
        logger.setLevel(getattr(logging, log_level, logging.INFO))

        log_format = log_config.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        log_file = log_config.get("file", None)

        formatter = logging.Formatter(log_format)

        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        else:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        return logger

    def start(self):
        """Start the supervisor."""

        # --- start heartbeat service ---
        self._heartbeat.start()
        # --- start device monitor service ---
        self._deviceMonitor.start()

    def stop(self):
        """Stop the supervisor."""

        # --- start heartbeat service ---
        self._heartbeat.stop()
