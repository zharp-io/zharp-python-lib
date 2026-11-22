"""The Cloud Functions that serverless_app.py runs through the Functions Framework."""

import functions_framework

import zharp


@functions_framework.http
@zharp.cloud_function
def orders(request):
    zharp.log.info("gcf ran " + request.path)
    if request.path == "/fail":
        raise SyntaxError("gcf boom")
    return {"ok": True}, 201


@functions_framework.cloud_event
@zharp.cloud_function
def on_message(cloud_event):
    zharp.log.info("event ran " + cloud_event["id"])
