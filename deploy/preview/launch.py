"""Preview assistant: the deployed core-api image, with the agent modules replaced by the working copy's
(copied to /tmp/preview). Serves the assistant-service app on :8080 for local testing of unmerged backend
changes against real data and the real model. Uses the repo prompt (not Langfuse's production version)."""
import importlib.util
import sys


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


import core_api.agent as pkg  # noqa: E402
import core_api.agent.prompts as prompts  # noqa: E402

prompts.get_prompt = lambda name, default: (default, "repo-preview")
pkg.tools = load("core_api.agent.tools", "/tmp/preview/tools.py")
pkg.loop = load("core_api.agent.loop", "/tmp/preview/loop.py")
pkg.history = load("core_api.agent.history", "/tmp/preview/history.py")

import uvicorn  # noqa: E402
from core_api.apps.assistant import app  # noqa: E402  (its router imports run_stream from the patched loop)

print("preview assistant: patched agent loaded", flush=True)
uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
