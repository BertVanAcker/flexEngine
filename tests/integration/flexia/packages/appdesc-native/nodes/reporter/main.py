"""Native runnable without an explicit entry (main.py is found by the importer)."""
import time

print("reporter started", flush=True)
while True:
    time.sleep(1)
