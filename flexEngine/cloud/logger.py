#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import logging
from flexEngine.communication.matrix import *

class remoteLogger(logging.Handler):
    def __init__(self, redis_client, primaryHardwareID="Oberon-5",projectID="sandbox",key="3fg445", level=logging.NOTSET):
        """
        Initialize the remote logger.

        :param redis_client: A Redis client instance.
        :param key: The Redis key used for the remote device logging.

        """
        super().__init__(level)
        self.redis = redis_client
        self.key = projectID + "-" + key + ":devices:" + primaryHardwareID+COMMUNICATIONMATRIX.LOG # projectname-key:devices:devicename:logs

    def emit(self, record):
        try:
            # Format the log record using the handler's formatter
            msg = self.format(record)
            # Push the log message to a Redis list
            self.redis.rpush(self.key, msg)
        except Exception:
            self.handleError(record)