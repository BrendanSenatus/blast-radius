"""Blast Radius demo API.

A deliberately small service for the lab to protect. /config reads a fake
secret mounted from a Kubernetes Secret, which later phases try to steal.
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException

SECRET_PATH = Path(os.getenv("API_KEY_FILE", "/etc/blast-radius/api-key"))

app = FastAPI(title="blast-radius-api")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/items")
def items():
    return [
        {"id": 1, "name": "widget"},
        {"id": 2, "name": "gadget"},
    ]


@app.get("/config")
def config():
    try:
        key = SECRET_PATH.read_text().strip()
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="api key not mounted")
    # Only show the last 4 characters, like a real app would.
    return {"api_key": "****" + key[-4:]}
