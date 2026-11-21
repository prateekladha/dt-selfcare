"""Process plumbing shared by the aggregator entrypoints: settings, signals, metrics, flags."""
from __future__ import annotations

import signal
import threading
import time

from prometheus_client import start_http_server
from pydantic_settings import BaseSettings, SettingsConfigDict
from telco_common.flags import flag_obj, init_flags


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    valkey_url: str = "redis://valkey:6379/0"
    metrics_port: int = 9000
    consumer_group: str = "usage-aggregator"
    batch_size: int = 200
    archive_bucket: str = "usage-raw-archive"
    archive_namespace: str = ""
    archive_flush_bytes: int = 4_000_000
    archive_flush_seconds: int = 60
    oci_auth: str = "instance_principal"  # instance_principal | config_file


class Shutdown:
    """SIGTERM-aware flag so pods drain cleanly on rollout / scale-in."""

    def __init__(self) -> None:
        self.event = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: self.event.set())
        signal.signal(signal.SIGINT, lambda *_: self.event.set())

    @property
    def requested(self) -> bool:
        return self.event.is_set()


class AlertLevels:
    """Alert thresholds come from the feature-flag service; cached briefly so the hot path
    does not make a flag RPC per message, but ops can still change levels at runtime."""

    def __init__(self, ttl_s: float = 30.0):
        self.ttl_s, self._at, self._levels = ttl_s, 0.0, [50, 80, 100]

    def get(self) -> list[int]:
        now = time.monotonic()
        if now - self._at > self.ttl_s:
            try:
                levels = [int(x) for x in flag_obj("usage-alert-thresholds").get("levels", [])]
                if levels and all(0 < x <= 100 for x in levels):
                    self._levels = sorted(levels)
            except Exception:  # noqa: BLE001, S110 - keep last good value; flags must never stop the pipeline
                pass
            self._at = now
        return self._levels


def bootstrap(settings: Settings) -> Shutdown:
    init_flags()
    start_http_server(settings.metrics_port)
    return Shutdown()


def oci_object_storage(settings: Settings):
    import oci

    if settings.oci_auth == "instance_principal":
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        return oci.object_storage.ObjectStorageClient(config={}, signer=signer)
    return oci.object_storage.ObjectStorageClient(oci.config.from_file())
