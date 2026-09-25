"""Trusted GET relay: stdout data crosses the Docker boundary, never a host socket."""

import base64
import http.client
import json
import sys

path = sys.argv[1]
if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n\\"):
    raise ValueError("invalid request path")
connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=8)
connection.request("GET", path)
response = connection.getresponse()
body = response.read(5_000_001)
if len(body) > 5_000_000:
    raise ValueError("response exceeds 5 MB")
print(
    json.dumps(
        {
            "status": response.status,
            "content_type": response.getheader("Content-Type"),
            "location": response.getheader("Location"),
            "body": base64.b64encode(body).decode(),
        }
    )
)
connection.close()
