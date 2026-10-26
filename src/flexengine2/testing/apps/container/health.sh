#!/bin/sh
read -r line
if [ -f /tmp/healthy ]; then body="ok $FLEX_APP $FLEX_VERSION"; code="200 OK"; else body="not ready"; code="503 Service Unavailable"; fi
printf 'HTTP/1.0 %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n%s' "$code" "${#body}" "$body"
