#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import time
import threading
from flexEngine.logging.logger import *
from flexEngine.communication.matrix import *
class heartbeat:
    """
    Sends a heartbeat by periodically setting a Redis key with a time-to-live (TTL).
    """
    def __init__(self, redis_client, primaryHardwareID="Oberon-5",projectID="sandbox",key="3fg445", heartbeat_interval=5, ttl=10):
        """
        Initialize the HeartbeatSender.

        :param redis_client: A Redis client instance.
        :param key: The Redis key used for the heartbeat.
        :param heartbeat_interval: Interval (in seconds) between heartbeats.
        :param ttl: Time-to-live (in seconds) for the heartbeat key.
        """
        self.redis_client = redis_client
        self.key = primaryHardwareID+"@"+projectID+"-"+key+COMMUNICATIONMATRIX.HEARTBEAT          #devicename@projectname-key:heartbeat
        self.heartbeat_interval = heartbeat_interval
        self.ttl = ttl
        self._stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        #custom logger
        self._logger = Logger(name="customLogger",verbose=False)

    def start(self):
        """Start sending heartbeats."""
        self._stop_event.clear()
        self.thread.start()
        self._logger.syslog(msg="HeartbeatSender started.")


    def stop(self):
        """Stop sending heartbeats."""
        self._stop_event.set()
        self.thread.join()
        self._logger.syslog(msg="HeartbeatSender stopped.")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                # Write the current timestamp as the value (could be any marker)
                current_time = time.time()
                self.redis_client.set(self.key, current_time, ex=self.ttl)
                self._logger.syslog(msg=f"Heartbeat sent at {current_time}.", level="DEBUG")
            except Exception as e:
                self._logger.syslog(msg="Error sending heartbeat.", level="ERROR")
            time.sleep(self.heartbeat_interval)