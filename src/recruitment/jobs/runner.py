import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from recruitment.adapters.mail_runtime import MailboxRuntime
from recruitment.adapters.providers import Providers
from recruitment.core.errors import ProviderError
from recruitment.ports.queue import Lease, LeaseLost, WorkerQueue
from recruitment.services.deletion import RetentionService
from recruitment.services.mail_intake import MailboxJobs
from recruitment.services.opening_preparation import OpeningPreparation
from recruitment.services.processing import CVProcessor

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, queue: WorkerQueue, providers: Providers) -> None:
        self.queue = queue
        self.processor = CVProcessor(queue, providers)
        self.openings = OpeningPreparation(queue, providers)
        self.retention = RetentionService(queue)
        providers.mailboxes = providers.mailboxes or MailboxRuntime(queue.settings)
        self.mailboxes = MailboxJobs(queue, providers, providers.mailboxes)

    @contextmanager
    def heartbeat(self, lease: Lease) -> Iterator[None]:
        stop = threading.Event()

        def pulse() -> None:
            while not stop.wait(max(0.2, self.queue.settings.lease_seconds / 3)):
                try:
                    if not self.queue.renew(lease):
                        return
                except Exception:
                    # Every write also checks the lease. A lost heartbeat cannot authorize a write.
                    logger.warning("job.heartbeat_failed job_id=%s", lease.id)
                    return

        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=5)

    def run_once(self) -> bool:
        self.queue.dispatch()
        lease = self.queue.claim()
        if not lease:
            return False
        try:
            with self.heartbeat(lease):
                if lease.attempts > self.queue.settings.max_job_attempts:
                    raise ProviderError("RETRY_EXHAUSTED")
                if lease.kind == "cv.uploaded":
                    self.processor.process(lease)
                elif lease.kind == "opening.prepare":
                    self.openings.process(lease)
                elif lease.kind == "retention.settings_changed":
                    self.retention.recalculate(lease)
                elif lease.kind == "retention.application_due":
                    self.retention.delete_application(lease)
                elif lease.kind == "retention.upload_due":
                    self.retention.delete_upload(lease)
                elif lease.kind == "retention.mail_due":
                    self.retention.delete_mail_source(lease)
                elif lease.kind == "mailbox.sync":
                    self.mailboxes.sync(lease)
                elif lease.kind == "mailbox.maintain":
                    self.mailboxes.maintain(lease)
                elif lease.kind == "mailbox.disconnect":
                    self.mailboxes.disconnect(lease)
                elif lease.kind == "mail.attachment":
                    self.mailboxes.attachment(lease)
                else:
                    raise ProviderError("UNKNOWN_JOB")
                self.queue.finish(lease)
        except LeaseLost:
            logger.warning("job.lease_lost job_id=%s", lease.id)
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else ProviderError("PROCESSING_FAILED")
            logger.warning(
                "job.failed job_id=%s code=%s type=%s", lease.id, error.code, type(exc).__name__
            )
            with suppress(LeaseLost):
                self.queue.fail(lease, error)
        return True
