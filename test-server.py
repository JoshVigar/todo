#!/usr/bin/env python3
"""Test server that uses local fixtures on port 6420 — doesn't touch the real app."""
import os
import sys
from pathlib import Path

os.environ["SERVE_TASKS_NO_WATCH"] = "1"

HERE = Path(__file__).parent

import importlib.util
spec = importlib.util.spec_from_file_location("st", HERE / "serve-tasks.py")
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)

# Override paths AFTER exec (exec sets them from Path.home())
st.JSON_FILE = HERE / "test-fixtures" / "tasks-live.json"
st.EMAIL_SNAPSHOT_FILE = HERE / "test-fixtures" / "email-triage.json"
st.EMAIL_DISMISSED_FILE = HERE / "test-fixtures" / "email-dismissed.jsonl"
st.EMAIL_CONVERTED_FILE = HERE / "test-fixtures" / "email-converted.jsonl"
st.GH_SUPPORT_SNAPSHOT_FILE = HERE / "test-fixtures" / "gh-support-triage.json"
st.GH_SUPPORT_DISMISSED_FILE = HERE / "test-fixtures" / "ghsupport-dismissed.jsonl"
st.SLACK_SNAPSHOT_FILE = HERE / "test-fixtures" / "slack-triage.json"
st.SLACK_DISMISSED_FILE = HERE / "test-fixtures" / "slack-dismissed.jsonl"
st.SLACK_CONVERTED_FILE = HERE / "test-fixtures" / "slack-converted.jsonl"
st.REQUEST_LOG = HERE / "test-fixtures" / "requests.log"
st.PORT = 6420

import http.server
server = http.server.ThreadingHTTPServer(("", 6420), st.Handler)
print(f"Test server on http://localhost:6420")
print(f"  Email view:      http://localhost:6420/?view=email")
print(f"  GH Support view: http://localhost:6420/?view=ghsupport")
print(f"  Dashboard:       http://localhost:6420/?view=dashboard")
print("Press Ctrl+C to stop")
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nStopped")
