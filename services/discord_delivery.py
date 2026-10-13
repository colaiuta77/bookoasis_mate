# 영속 Discord 대기열을 스캔과 독립적으로 묶어 발송하고 재시도합니다.
import threading
from datetime import datetime, timedelta

from .discord_notifier import DiscordWebhookNotifier, DiscordWebhookError


class DiscordDeliveryWorker:
    def __init__(self, model, settings_provider, notifier_factory=DiscordWebhookNotifier, clock=None):
        self.model, self.settings_provider, self.notifier_factory = model, settings_provider, notifier_factory
        self.clock = clock or datetime.now
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.last_error = ''
        self.recover_results = None
        self.discard_results = None

    def change_destination(self, save):
        with self.lock:
            old = self.model.fingerprint(self.settings_provider().get('gdrive_scan_discord_webhook_url'))
            if old:
                if self.discard_results:
                    self.discard_results(old)
                self.model.cancel_pending(old, self.clock())
            save()
            current = self.model.fingerprint(self.settings_provider().get('gdrive_scan_discord_webhook_url'))
            if current:
                self.model.set_destination_limit(current)
        self.wake()

    def run_once(self):
        with self.lock:
            if self.recover_results:
                self.recover_results()
            now = self.clock()
            settings = self.settings_provider()
            url = settings.get('gdrive_scan_discord_webhook_url') or ''
            destination = self.model.fingerprint(url)
            self.model.reconcile_destination(destination, now)
            quiet = max(0, min(86400, int(settings.get('gdrive_scan_discord_batch_seconds', 60))))
            maximum = max(quiet, min(86400, int(settings.get('gdrive_scan_discord_max_wait_seconds', 180))))
            delivery = self.model.claim(now, quiet, maximum, destination)
            if not delivery:
                return False
            notifier = self.notifier_factory(url)
            payload = delivery['payload']
            if payload is None:
                if delivery['kind'] == 'health':
                    payload = notifier.build_health_payload(**delivery['items'][0])
                else:
                    items = delivery['items']
                    events = [dict(item['event'], id=index) for index, item in enumerate(items)]
                    payload = notifier.build_batch_payload(events,
                        {index: item['result'] for index, item in enumerate(items)},
                        {index: item['status'] for index, item in enumerate(items)})
                self.model.save_payload(delivery['id'], payload)
        # HTTP 요청 중에는 DB 트랜잭션이나 설정 잠금을 점유하지 않습니다.
        try:
            response = notifier.send_payload(payload, wait=True)
        except DiscordWebhookError as error:
            with self.lock:
                if self.model.is_sending(delivery['id']):
                    self._retry(delivery, destination, error)
        else:
            self.model.complete(delivery['id'], response['message_id'], self.clock())
        return True

    def _retry(self, delivery, destination, error):
        now, code = self.clock(), error.status_code
        if code == 429:
            due = now + timedelta(seconds=max(1, error.retry_after if error.retry_after is not None else 30))
            self.model.set_destination_limit(destination, until=due, error=str(error))
            self.model.reschedule(delivery['id'], due, str(error), False)
        elif code in (401, 403, 404):
            self.model.set_destination_limit(destination, blocked=True, error=str(error))
            self.model.fail(delivery['id'], str(error), now)
        elif (code is not None and 400 <= code < 500) or delivery['attempts'] >= 9:
            self.model.fail(delivery['id'], str(error), now)
        else:
            due = now + timedelta(seconds=min(1800, 30 * 2 ** delivery['attempts']))
            self.model.reschedule(delivery['id'], due, str(error), True)

    def _run(self):
        recovered = False
        cleanup_at = datetime.min
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if not recovered:
                    self.model.recover(self.clock())
                    recovered = True
                if self.clock() >= cleanup_at:
                    self.model.cleanup(self.clock())
                    cleanup_at = self.clock() + timedelta(hours=1)
                worked = self.run_once()
                self.last_error = ''
            except Exception as error:
                self.last_error = f'알림 작업자 저장/처리 오류 ({type(error).__name__})'
                recovered, worked = False, False
            self._wake.wait(1 if worked else 5)

    def start(self):
        with self.lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name='mate-discord', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self.wake()
        if self._thread:
            self._thread.join(timeout=6)

    def wake(self):
        self._wake.set()
