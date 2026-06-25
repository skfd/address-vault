"""address-vault: a single-host, tiered store for raw city address dumps.

The library *is* the API -- consumers ``from addressvault import Vault`` and read
dated snapshots; a thin CLI (``addressvault``) wraps the same calls for cron and
humans. Snapshots live in a hot tier (on disk) and a restic cold tier (kept
forever); thaw copies a cold day back to hot on demand.
"""

from addressvault.sources import Source
from addressvault.vault import Archived, Snapshot, Vault

__all__ = ["Vault", "Snapshot", "Source", "Archived"]
