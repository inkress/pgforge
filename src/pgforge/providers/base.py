"""Provider ABC and the typed dataclasses that flow across it.

Design notes:

* Every method that talks to a cloud may raise :class:`pgforge.errors.ProviderError`
  (or a subclass). Concrete providers translate CLI exit codes / stderr into
  these typed errors.

* Returned dataclasses always carry a ``raw: dict`` field with the original
  CLI JSON. Useful for ``pgforge show --json`` and for debugging when an
  abstraction leaks.

* Method signatures intentionally use plain strings/ints (not pydantic) so
  call sites don't need to import schemas; pydantic only matters for the
  on-disk state file.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar


# ---------------------------------------------------------------------------
# Plain dataclasses returned across the abstraction
# ---------------------------------------------------------------------------


@dataclass
class CLIInfo:
    binary: str
    """Resolved path or name of the provider CLI."""

    version: str
    """Version string as reported by the CLI."""

    authenticated: bool
    """True if a whoami-equivalent succeeded."""

    account: str | None = None
    """Human-readable account/context identifier, if available."""

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Location:
    name: str
    description: str | None = None
    country: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Server:
    id: str
    name: str
    ipv4: str | None
    ipv6: str | None
    location: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SSHEndpoint:
    host: str
    """IPv4 if present, otherwise IPv6. Callers must quote IPv6 properly."""

    port: int = 22
    user: str = "root"


@dataclass
class VolumeSpec:
    name: str
    size_gb: int
    location: str
    labels: dict[str, str] = field(default_factory=dict)
    server_id: str | None = None
    """If set, the provider may create-and-attach in one call where supported."""


@dataclass
class Volume:
    id: str
    name: str
    size_gb: int
    location: str
    attached_server_id: str | None
    labels: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttachedVolume:
    volume: Volume
    server_id: str
    device_path: str
    """The path the kernel exposes on the server, e.g. ``/dev/disk/by-id/...``."""


@dataclass
class Snapshot:
    id: str
    name: str
    source_volume_id: str
    size_gb: int
    created_at: datetime
    labels: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TimeWindow:
    start: datetime
    end: datetime

    @classmethod
    def last(cls, duration: timedelta) -> "TimeWindow":
        from datetime import timezone

        end = datetime.now(timezone.utc)
        return cls(start=end - duration, end=end)


@dataclass
class MetricSeries:
    points: list[tuple[datetime, float]]
    unit: str
    unavailable_reason: str | None = None
    """Set when the provider cannot return this series. ``points`` is then empty."""


@dataclass
class ProviderMetrics:
    volume_id: str
    window: TimeWindow
    iops_read: MetricSeries | None = None
    iops_write: MetricSeries | None = None
    throughput_read_bps: MetricSeries | None = None
    throughput_write_bps: MetricSeries | None = None
    queue_depth: MetricSeries | None = None
    latency_ms: MetricSeries | None = None
    provider_native: dict[str, Any] = field(default_factory=dict)


@dataclass
class CapacityInfo:
    volume_id: str
    provider_reported_size_bytes: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SnapshotScope:
    """Inputs for minting a server-side credential limited to snapshot ops."""

    instance_name: str
    volume_id: str
    server_id: str
    location: str


@dataclass
class RemoteCredential:
    """A credential pgforge installs on the DB server so cron can call the cloud CLI.

    ``mode`` describes how the cred is delivered:

    - ``instance_role`` — the provider attaches an identity to the server
      (IAM role / managed identity / service account). No secret on disk.
      ``install_steps`` may contain remote shell commands to apply.
    - ``file_credential`` — a token must be written to a file on the server.
      ``files`` maps path -> content (mode 0400).
    - ``env_credential`` — the cred lives in environment variables in the
      cron file. Pulled from ``files`` (path empty, env_export populated).
    """

    credential_id: str
    mode: str  # "instance_role" | "file_credential"
    install_steps: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    env_exports: dict[str, str] = field(default_factory=dict)
    last4: str = ""
    """Last four characters of the secret, for state metadata. Never the full secret."""


# ---------------------------------------------------------------------------
# Abstract Provider
# ---------------------------------------------------------------------------


class Provider(ABC):
    """Contract every cloud provider implementation fulfills.

    Naming convention: subclasses live in ``pgforge.providers.<name>`` and set
    ``name`` to the registry key (``hetzner``, ``aws``, ``gcp``, ``azure``,
    ``digitalocean``, ``linode``).
    """

    name: ClassVar[str]
    cli_binary: ClassVar[str]
    min_cli_version: ClassVar[str]

    # ---- preflight ----

    @abstractmethod
    def check_cli(self) -> CLIInfo:
        """Verify the provider CLI is installed, in version, and authenticated."""

    @abstractmethod
    def list_locations(self) -> list[Location]:
        """Return all locations/regions the user can deploy into."""

    # ---- server ----

    @abstractmethod
    def get_server(self, server_id_or_name: str) -> Server:
        """Resolve a server by id or by name. Raises ProviderResourceNotFound."""

    def server_ssh_endpoint(self, server: Server) -> SSHEndpoint:
        """IPv4-preferred SSH endpoint. Override only if the provider needs custom logic."""
        host = server.ipv4 or server.ipv6
        if host is None:
            from pgforge.errors import ProviderAPIError

            raise ProviderAPIError(f"server {server.id} has no IPv4 or IPv6 address")
        return SSHEndpoint(host=host)

    @abstractmethod
    def server_device_path(self, server: Server, volume: Volume) -> str:
        """Compute the device path the kernel will expose for ``volume`` on ``server``.

        Each provider names attached volumes differently:

        * Hetzner: ``/dev/disk/by-id/scsi-0HC_Volume_<id>``
        * AWS Nitro: ``/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_<volid>``
        * GCP: ``/dev/disk/by-id/google-<device-name>``
        * Azure: ``/dev/disk/azure/scsi1/lun<N>``
        * DigitalOcean: ``/dev/disk/by-id/scsi-0DO_Volume_<name>``
        * Linode: ``/dev/disk/by-id/scsi-0Linode_Volume_<label>``
        """

    # ---- volumes ----

    @abstractmethod
    def create_volume(self, spec: VolumeSpec) -> Volume: ...

    @abstractmethod
    def attach_volume(self, volume_id: str, server_id: str) -> AttachedVolume: ...

    @abstractmethod
    def detach_volume(self, volume_id: str) -> None: ...

    @abstractmethod
    def delete_volume(self, volume_id: str) -> None: ...

    @abstractmethod
    def get_volume(self, volume_id: str) -> Volume: ...

    def find_volume_by_name(self, name: str) -> Volume | None:
        """Optional helper; default scans :meth:`list_volumes`."""
        for v in self.list_volumes():
            if v.name == name:
                return v
        return None

    @abstractmethod
    def list_volumes(self) -> list[Volume]: ...

    # ---- snapshots ----

    @abstractmethod
    def create_snapshot(
        self, volume_id: str, name: str, labels: dict[str, str] | None = None
    ) -> Snapshot: ...

    @abstractmethod
    def list_snapshots(self, volume_id: str | None = None) -> list[Snapshot]: ...

    @abstractmethod
    def delete_snapshot(self, snapshot_id: str) -> None: ...

    @abstractmethod
    def restore_snapshot(self, snapshot_id: str, target_spec: VolumeSpec) -> Volume: ...

    # ---- metrics & capacity ----

    @abstractmethod
    def volume_metrics(self, volume_id: str, window: TimeWindow) -> ProviderMetrics:
        """Provider-side metrics. Implementations that don't expose a series
        should return :class:`MetricSeries` with ``unavailable_reason`` set."""

    @abstractmethod
    def volume_capacity(self, volume_id: str) -> CapacityInfo:
        """Block-device size as reported by the provider. Filesystem-level usage
        is collected separately via SSH (see ``metrics/collector.py``)."""

    # ---- snapshot cron credentials ----

    @abstractmethod
    def mint_snapshot_credential(self, scope: SnapshotScope) -> RemoteCredential: ...

    @abstractmethod
    def revoke_snapshot_credential(self, credential_id: str) -> None: ...
