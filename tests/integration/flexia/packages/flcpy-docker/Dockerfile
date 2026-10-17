# flcpy node, v1 style: the image installs flexCommunicator itself. The test puts the wheels into ./wheels
# (pip download flexCommunicator) so the build needs no network.
FROM mirror.gcr.io/library/python:3.13-alpine
COPY wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels flexCommunicator && rm -rf /wheels
WORKDIR /app
COPY publisher.py /app/
CMD ["python", "-u", "publisher.py"]
