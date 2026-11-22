"""RQ jobs for frameworks_app.py: RQ imports a job's function by its module path."""


def ok_job(n):
    return n * 2


def boom_job():
    raise ValueError("rq boom")
