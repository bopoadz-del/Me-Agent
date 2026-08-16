"""Block registry for orchestrator and spawner."""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from common.models.schemas import BlockDef


class BlockRegistry:
    def __init__(self) -> None:
        self._blocks: dict[str, BlockDef] = {}
        self._runners: dict[str, Callable[..., dict[str, Any]]] = {}

    def register(self, block: BlockDef) -> None:
        self._blocks[block.block_id] = block

    def add(self, block: BlockDef) -> None:
        self.register(block)

    def get(self, block_id: str) -> Optional[BlockDef]:
        return self._blocks.get(block_id)

    def all_blocks(self) -> list[BlockDef]:
        return list(self._blocks.values())

    def blocks_for_domain(self, domain: str) -> list[BlockDef]:
        return [b for b in self._blocks.values() if b.domain == domain]

    def register_runner(self, block_id: str, fn: Callable[..., dict[str, Any]]) -> None:
        """Declared test seam (Rule 3 / A3). Forbidden unless TEST_MODE=true."""
        if os.environ.get("TEST_MODE", "").lower() != "true":
            raise RuntimeError(
                "register_runner is a test seam and requires TEST_MODE=true"
            )
        self._runners[block_id] = fn

    def set_runner(self, block_id: str, fn: Callable[..., dict[str, Any]]) -> None:
        self.register_runner(block_id, fn)

    def runner_for(self, block_id: str) -> Optional[Callable[..., dict[str, Any]]]:
        return self._runners.get(block_id)

    def __getitem__(self, block_id: str) -> BlockDef:
        return self._blocks[block_id]

    def __contains__(self, block_id: str) -> bool:
        return block_id in self._blocks
