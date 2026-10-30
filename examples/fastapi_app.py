"""A FastAPI app with Zharp. Run it with the key in the environment:

    pip install zharp fastapi uvicorn
    ZHARP_KEY=zpk_… ZHARP_ENDPOINT=http://localhost:3000/api/v1/otlp uvicorn fastapi_app:app --port 8000

then: curl localhost:8000/hello/ann  ·  curl localhost:8000/boom
"""

import logging

import zharp

zharp.init(service="python-example", environment="development")  # key etc. from ZHARP_* variables

from fastapi import FastAPI  # noqa: E402

app = FastAPI()
log = logging.getLogger("example")


@app.get("/hello/{name}")
def hello(name: str):
    log.info("Saying hello to %s", name)
    return {"hello": name}


@app.get("/boom")
def boom():
    log.warning("About to fail")
    raise RuntimeError("Something broke in /boom")
