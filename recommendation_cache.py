"""Small stale-while-revalidate cache; no candidate SQL runs on a request thread."""
import logging
import threading
import time


class RecommendationCache:
    def __init__(self, builder, debounce=3.0, ttl=300.0, retry=30.0):
        self.builder, self.debounce, self.ttl, self.retry = builder, debounce, ttl, retry
        self.condition = threading.Condition()
        self.entries, self.wanted, self.pending, self.errors = {}, {}, {}, {}
        self.inflight = None
        self.thread = None
        self.closed = False

    def get(self, key, per_source):
        """Return a previous result immediately, scheduling at most one SQL build."""
        with self.condition:
            now = time.monotonic()
            entry = self.entries.get(per_source)
            fresh = bool(entry and entry[0] == key and now - entry[1] < self.ttl)
            error = self.errors.get(per_source)
            cooling = bool(error and error[0] == key and now - error[1] < self.retry)
            self.wanted[per_source] = key
            if fresh or self.inflight == (per_source, key):
                self.pending.pop(per_source, None)
            if not fresh and not cooling and not self.closed:
                pending = self.pending.get(per_source)
                if self.inflight != (per_source, key) and (not pending or pending[0] != key):
                    self.pending[per_source] = (key, now + self.debounce)
                if not self.thread or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self._run, name='recommendation-sql', daemon=True)
                    self.thread.start()
                self.condition.notify_all()
            return (entry[2] if entry else None), {
                'status': 'ready' if fresh else 'failed' if cooling else 'refreshing',
                'stale': bool(entry and not fresh),
                'message': '추천 재계산에 실패했습니다. 잠시 후 다시 시도합니다.' if cooling else '',
            }

    def _run(self):
        while True:
            with self.condition:
                if self.closed or not self.pending:
                    # Clear under the same lock used by get(), avoiding a lost wakeup.
                    self.thread = None
                    return
                per_source, (key, due) = min(self.pending.items(), key=lambda pair: pair[1][1])
                delay = due - time.monotonic()
                if delay > 0:
                    self.condition.wait(timeout=delay)
                    continue
                self.pending.pop(per_source)
                self.inflight = (per_source, key)
            try:
                result = self.builder(per_source)
            except Exception:
                logging.getLogger(__name__).exception('Background recommendation SQL failed')
                with self.condition:
                    self.errors[per_source] = (key, time.monotonic())
            else:
                with self.condition:
                    if self.wanted.get(per_source) == key:
                        self.entries[per_source] = (key, time.monotonic(), result)
                        self.errors.pop(per_source, None)
            finally:
                with self.condition:
                    self.inflight = None
                    self.condition.notify_all()

    def close(self):
        """Stop pending work; an already-running read-only query may finish."""
        with self.condition:
            self.closed = True
            self.condition.notify_all()
