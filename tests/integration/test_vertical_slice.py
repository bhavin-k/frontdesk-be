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
    assert result["criteria"][0]["evidence"][0]["quote"] in cv_text()


def cv_text() -> str:
    return "Python development: Built backend APIs for three years."


def test_worker_end_to_end(context: Context, cv: bytes) -> None:
    response = context.upload(cv)
    assert response.status_code == 202
    context.drain()
    result = context.result(response.json()["upload_id"])
    assert result["provider"] == "fake"
    assert result["embedding_dimensions"] == 1536
    assert result["model"] == "fake-grader-v1"
    assert result["prompt_version"] == "matching-v1"
