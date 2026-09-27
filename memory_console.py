"""Loch Raven Institutional Memory console, standalone.

  py -3.11 memory_console.py        ->  http://localhost:8421/memory

Runs only the memory layer built at hackUMBC 2026. No broker, no trading
threads, no scheduled cycles, so it is safe to run next to a live firm and it
works offline from the bundled archive in data/memory.
"""
import argparse

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from firm import memory_api

app = FastAPI(title="Loch Raven Institutional Memory")
app.include_router(memory_api.router)


@app.get("/")
def root():
    return RedirectResponse("/memory")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8421)
    args = ap.parse_args()
    memory_api.warm_up()
    print(f"Loch Raven Institutional Memory -> http://localhost:{args.port}/memory")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
