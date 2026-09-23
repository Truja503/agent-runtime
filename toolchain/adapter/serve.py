"""Fixed Flask entrypoint, project-local interpreter, no debug/reloader."""

import importlib.util
import sys

from flask import Flask
from werkzeug.serving import make_server

sys.path.insert(0, "/project")
spec = importlib.util.spec_from_file_location("generated_application", "/project/app.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
app = module.app
if not isinstance(app, Flask):
    raise TypeError("app.py must export a Flask instance named app")
app.debug = False
make_server("127.0.0.1", 8080, app, threaded=True).serve_forever()
