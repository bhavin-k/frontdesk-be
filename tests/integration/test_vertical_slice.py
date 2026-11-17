from recruitment.adapters.queue import PostgresJobQueue
from tests.support import Context


def test_processor_transaction_path(context: Context, cv: bytes) -> None:
    response = context.upload(cv)
    assert response.status_code == 202
    worker = context.worker()
    queue: PostgresJobQueue = worker.queue
    queue.dispatch()
    lease = queue.claim()
    assert lease is not None
    worker.processor.process(lease)
    queue.finish(lease)
    result = context.result(response.json()["upload_id"])
    assert result["score"] == 100
    assert result["recommendation"] == "recommended_shortlist"
    assert result["fit"] and result["matched"] == ["Python"]


def test_worker_end_to_end(context: Context, cv: bytes) -> None:
    response = context.upload(cv)
    assert response.status_code == 202
    context.drain()
    result = context.result(response.json()["upload_id"])
    assert result["fit"]
    assert result["exp_ok"]
    assert result["score"] == 100
