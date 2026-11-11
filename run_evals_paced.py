"""Run the original evaluator with shared API pacing for agent and judge.

Use the same options as evals/run_evals.py. Extra configuration uses env vars:
API_INTERVAL_SECONDS=30, API_MAX_RETRIES=2, API_RETRY_BASE_SECONDS=60.
Writes a separate <output>.api.jsonl transport log; never edits eval scores.
"""
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from datetime import datetime, timezone


class PacedCalls:
    def __init__(self, complete, interval=30, retries=2, retry_base=60,
                 clock=time.monotonic, sleep=time.sleep, emit=None):
        self.complete = complete
        self.interval = interval
        self.retries = retries
        self.retry_base = retry_base
        self.clock, self.sleep = clock, sleep
        self.emit = emit or (lambda event: None)
        self.next_start = 0
        self.lock = threading.Lock()

    def __call__(self, provider, messages, schemas):
        # One shared lock across every Provider instance, including the judge.
        with self.lock:
            for attempt in range(self.retries + 1):
                delay = max(0, self.next_start - self.clock())
                if delay:
                    print(f'[API] Waiting {delay:.1f}s', flush=True)
                    self.sleep(delay)
                self.next_start = self.clock() + self.interval
                event = {'time': datetime.now(timezone.utc).isoformat(),
                         'provider': provider.name, 'model': provider.model,
                         'attempt': attempt + 1}
                try:
                    result = self.complete(provider, messages, schemas)
                except Exception as exc:
                    code = getattr(exc.__cause__, 'code', None)
                    if code is None:
                        match = re.search(r'\bHTTP\s+(\d{3})\b', str(exc))
                        code = int(match.group(1)) if match else None
                    retry = code in (429, 500, 502, 503, 504) and attempt < self.retries
                    self.emit({**event, 'status': 'error', 'http_code': code,
                               'will_retry': retry})
                    if not retry:
                        raise
                    cooldown = self.retry_base * (2 ** attempt)
                    headers = getattr(exc.__cause__, 'headers', None)
                    if headers:
                        try:
                            value = float(headers.get('Retry-After', '0'))
                            if math.isfinite(value):
                                cooldown = max(cooldown, value)
                        except (TypeError, ValueError):
                            pass
                    self.next_start = max(self.next_start, self.clock() + cooldown)
                    print(f'[API] HTTP {code}; retry {attempt + 1}/{self.retries}', flush=True)
                else:
                    self.emit({**event, 'status': 'success'})
                    return result


def main():
    interval = float(os.getenv('API_INTERVAL_SECONDS', '30'))
    retries = int(os.getenv('API_MAX_RETRIES', '2'))
    retry_base = float(os.getenv('API_RETRY_BASE_SECONDS', '60'))
    if (not math.isfinite(interval) or interval < 0 or retries < 0
            or not math.isfinite(retry_base) or retry_base < 0):
        raise ValueError('API pacing settings must be finite and nonnegative.')
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / 'evals'))
    import run_evals
    from providers import Provider

    output = 'eval_results.json'
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--output' and i + 1 < len(sys.argv):
            output = sys.argv[i + 1]
        elif arg.startswith('--output='):
            output = arg.split('=', 1)[1]
    log_path = Path(output + '.api.jsonl')
    with log_path.open('a', encoding='utf-8') as log:
        def emit(event):
            log.write(json.dumps(event, ensure_ascii=False) + '\n')
            log.flush()
        emit({'event': 'transport_config', 'interval_seconds': interval,
              'max_retries': retries, 'retry_base_seconds': retry_base})
        original = Provider.complete
        paced = PacedCalls(original, interval, retries, retry_base, emit=emit)
        def complete(provider, messages, schemas):
            return paced(provider, messages, schemas)
        Provider.complete = complete
        try:
            return run_evals.main()
        finally:
            Provider.complete = original


if __name__ == '__main__':
    sys.exit(main())
