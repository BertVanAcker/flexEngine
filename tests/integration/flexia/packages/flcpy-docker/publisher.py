"""flcpy publisher stand-in (flexCommunicator examples/GettingStarted/pubsub_data): builds the same DataMessage the
GettingStarted publisher sends every 0.5 s. It does not connect to a broker (the test has none); it proves that the
flexCommunicator client library is installed in the release image and that the node keeps running. Like every v1
flexNode it serves no health endpoint: the importer wraps it with the liveness shim."""

import os
import random
import time

from flexCommunicator.clientLibraries.flcpy.messages.StandardizedMessages import DataMessage

node = os.environ.get("FLEX_NODE_ID", "publisher")
print(f"{node}: flexCommunicator DataMessage available, publishing loop started", flush=True)
n = 0
while True:
    msg = DataMessage()
    msg.dataType, msg.dataUnit, msg.value = "Float", "m", round(random.uniform(0, 10), 3)
    n += 1
    if n % 20 == 1:
        print(f"{node}: CustomMessage #{n} value={msg.value}", flush=True)
    time.sleep(0.5)
