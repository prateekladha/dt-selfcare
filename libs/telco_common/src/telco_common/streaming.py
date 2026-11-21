"""Kafka-protocol client config for OCI Streaming.

OCI Streaming exposes a Kafka-compatible endpoint: SASL_SSL + PLAIN where the username is
`<tenancy>/<user>/<stream-pool-ocid>` and the password is an OCI auth token. Keeping this in one
place means services only see `bootstrap/topic` and could move to any Kafka with config only.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class StreamSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STREAM_")

    bootstrap: str = "cell-1.streaming.ap-mumbai-1.oci.oraclecloud.com:9092"
    username: str = ""
    password: str = ""
    usage_topic: str = "usage-events"
    alerts_topic: str = "usage-alerts"
    partitions: int = 4
    security_protocol: str = "SASL_SSL"

    def client_config(self) -> dict[str, str]:
        cfg = {"bootstrap.servers": self.bootstrap, "security.protocol": self.security_protocol}
        if self.security_protocol.startswith("SASL"):
            cfg |= {"sasl.mechanisms": "PLAIN", "sasl.username": self.username, "sasl.password": self.password}
        return cfg
