"""Bounded, owner-scoped render scheduling for one application process."""

import logging
import secrets
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable


class QueueFull(RuntimeError):
    pass


class QueueConflict(RuntimeError):
    pass


class QueueFailure(RuntimeError):
    def __init__(self, message, status=422, field_id=None, *, superseded=False):
        super().__init__(message)
        self.status = status
        self.field_id = field_id
        self.superseded = superseded


@dataclass
class Task:
    id: str
    owner: str
    key: str
    fingerprint: str
    operation: Callable | None
    coalesce: str | None
    revision: int | None
    submitted: float
    state: str = 'queued'
    started: float | None = None
    finished: float | None = None
    obsolete: bool = False
    result: dict | None = None
    failure: QueueFailure | None = None


class RenderQueue:
    def __init__(self, workers, capacity=16, per_owner=3, wait_timeout=120, retention=600):
        if min(workers, capacity, per_owner, wait_timeout, retention) <= 0:
            raise ValueError('Render queue settings must be positive.')
        self.workers = workers
        self.capacity = capacity
        self.per_owner = per_owner
        self.wait_timeout = wait_timeout
        self.retention = retention
        self.terminal_limit = max(64, capacity * 8)
        self.ready = OrderedDict()
        self.tasks = {}
        self.keys = {}
        self.latest = {}
        self.terminal = OrderedDict()
        self.active = 0
        self.waiting = 0
        self.last_owner = None
        self.condition = threading.Condition()
        for index in range(workers):
            threading.Thread(target=self._run, name=f'detail-render-{index + 1}', daemon=True).start()

    def _remove_waiting(self, task):
        pending = self.ready[task.owner]
        pending.remove(task.id)
        self.waiting -= 1
        if not pending:
            del self.ready[task.owner]

    def _finish(self, task, result=None, failure=None):
        task.finished = time.monotonic()
        if task.obsolete or (failure and failure.superseded):
            task.state = 'superseded'
            task.failure = QueueFailure('已有更新的文案，请等待最新预览。', status=409, superseded=True)
        elif failure is not None:
            task.state = 'failed'
            task.failure = failure
        else:
            task.state = 'complete'
            task.result = result
        task.operation = None
        self.terminal[task.id] = task.finished

    def _mark_obsolete(self, task):
        if task.state == 'queued':
            self._remove_waiting(task)
            task.obsolete = True
            self._finish(task)
        elif task.state == 'running':
            task.obsolete = True

    def _expire(self, now):
        for pending in list(self.ready.values()):
            for task_id in list(pending):
                task = self.tasks[task_id]
                if now - task.submitted >= self.wait_timeout:
                    self._remove_waiting(task)
                    self._finish(task, failure=QueueFailure('排队等待超时，请重试。', status=504))
        while self.terminal:
            task_id, finished = next(iter(self.terminal.items()))
            if len(self.terminal) <= self.terminal_limit and now - finished < self.retention:
                break
            self.terminal.popitem(last=False)
            task = self.tasks.pop(task_id)
            del self.keys[task.owner, task.key]
            group = (task.owner, task.coalesce)
            if self.latest.get(group) == task_id:
                del self.latest[group]

    def _snapshot(self, task, now):
        started = task.started
        report = {
            'id': task.id,
            'state': task.state,
            'wait_seconds': round((started if started is not None else task.finished or now) - task.submitted, 3),
        }
        if started is not None:
            report['render_seconds'] = round((task.finished or now) - started, 3)
        if task.result is not None:
            report['result'] = task.result
        if task.failure is not None:
            report.update(error=str(task.failure), status_code=task.failure.status)
            if task.failure.field_id is not None:
                report['field_id'] = task.failure.field_id
        return report

    def _replay(self, owner, key, fingerprint, now):
        previous = self.keys.get((owner, key))
        if previous is None:
            return None
        task = self.tasks[previous]
        if task.fingerprint != fingerprint:
            raise QueueConflict('该提交编号已用于其他内容，请重新提交。')
        return self._snapshot(task, now)

    def replay(self, owner, key, fingerprint):
        with self.condition:
            now = time.monotonic()
            self._expire(now)
            return self._replay(owner, key, fingerprint, now)

    def submit(self, owner, operation, *, key, fingerprint, coalesce=None, revision=None):
        if coalesce is not None and revision is None:
            raise ValueError('Coalescing requires a revision.')
        with self.condition:
            now = time.monotonic()
            self._expire(now)
            if previous := self._replay(owner, key, fingerprint, now):
                return previous

            group = (owner, coalesce)
            if coalesce is not None and group in self.latest:
                old = self.tasks[self.latest[group]]
                if revision <= old.revision:
                    raise QueueConflict('草稿已有更新，请等待最新预览。')
                self._mark_obsolete(old)

            if self.waiting >= self.capacity:
                raise QueueFull('排队人数较多，请稍后重试。')
            if len(self.ready.get(owner, ())) >= self.per_owner:
                raise QueueFull('你已有多项任务在排队，请等当前任务完成后重试。')

            task = Task(secrets.token_hex(16), owner, key, fingerprint, operation, coalesce, revision, now)
            self.tasks[task.id] = task
            self.keys[owner, key] = task.id
            if coalesce is not None:
                self.latest[group] = task.id
            self.ready.setdefault(owner, deque()).append(task.id)
            self.waiting += 1
            self._expire(now)
            self.condition.notify_all()
            return self._snapshot(task, now)

    def get(self, owner, task_id):
        with self.condition:
            now = time.monotonic()
            self._expire(now)
            task = self.tasks.get(task_id)
            if task is None or task.owner != owner:
                return None
            return self._snapshot(task, now)

    def supersede(self, owner, coalesce, *, before_revision):
        with self.condition:
            now = time.monotonic()
            self._expire(now)
            task_id = self.latest.get((owner, coalesce))
            if task_id is not None:
                task = self.tasks[task_id]
                if task.revision < before_revision:
                    self._mark_obsolete(task)
                    self._expire(now)
                    self.condition.notify_all()

    @contextmanager
    def immediate(self):
        """Share a free worker with a synchronous compatibility request."""
        with self.condition:
            self._expire(time.monotonic())
            if self.active >= self.workers or self.ready:
                raise QueueFull('当前有排版任务，请稍后重试。')
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def _take(self):
        with self.condition:
            while True:
                now = time.monotonic()
                self._expire(now)
                if self.ready and self.active < self.workers:
                    break
                self.condition.wait()
            owner = next(iter(self.ready))
            if owner == self.last_owner and len(self.ready) > 1:
                self.ready.move_to_end(owner)
                owner = next(iter(self.ready))
            pending = self.ready.pop(owner)
            task = self.tasks[pending.popleft()]
            if pending:
                self.ready[owner] = pending
            self.last_owner = owner
            self.waiting -= 1
            self.active += 1
            task.state = 'running'
            task.started = now
            return task

    def _run(self):
        while True:
            task = self._take()
            result = failure = None
            try:
                result = task.operation()
            except QueueFailure as error:
                failure = QueueFailure(str(error), error.status, error.field_id, superseded=error.superseded)
            except Exception:
                logging.getLogger(__name__).exception('Render task %s failed', task.id)
                failure = QueueFailure('排版失败，请重试。', status=500)
            finally:
                with self.condition:
                    self.active -= 1
                    self._finish(task, result, failure)
                    self._expire(time.monotonic())
                    self.condition.notify_all()
