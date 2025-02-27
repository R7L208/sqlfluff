"""The DepthMap class is an enriched sequence of raw segments."""

import logging
from dataclasses import dataclass
from typing import FrozenSet, List, Sequence, Tuple, Type

from sqlfluff.core.parser import BaseSegment
from sqlfluff.core.parser.segments.base import PathStep
from sqlfluff.core.parser.segments.raw import RawSegment


reflow_logger = logging.getLogger("sqlfluff.rules.reflow")


def _stack_pos_interpreter(path_step: PathStep):
    """Interpret a path step for stack_positions."""
    if path_step.idx == 0 and path_step.idx == path_step.len - 1:
        return "solo"
    elif path_step.idx == 0:
        return "start"
    elif path_step.idx == path_step.len - 1:
        return "end"
    else:
        return ""  # NOTE: Empty string evaluates is falsy.


@dataclass(frozen=True)
class DepthInfo:
    """An object to hold the depth information for a specific raw segment."""

    stack_depth: int
    stack_hashes: Tuple[int, ...]
    # This is a convenience cache to speed up operations.
    stack_hash_set: FrozenSet[int]
    stack_class_types: Tuple[FrozenSet[str], ...]
    stack_positions: Tuple[str, ...]

    @classmethod
    def from_raw_and_stack(cls, raw: RawSegment, stack: Sequence[PathStep]):
        """Construct from a raw and its stack."""
        stack_hashes = tuple(hash(ps.segment) for ps in stack)
        return cls(
            stack_depth=len(stack),
            stack_hashes=stack_hashes,
            stack_hash_set=frozenset(stack_hashes),
            stack_class_types=tuple(frozenset(ps.segment.class_types) for ps in stack),
            stack_positions=tuple(_stack_pos_interpreter(ps) for ps in stack),
        )

    def common_with(self, other: "DepthInfo") -> Tuple[int, ...]:
        """Get the common depth and hashes with the other."""
        # We use set intersection because it's faster and hashes should be unique.
        common_hashes = self.stack_hash_set.intersection(other.stack_hashes)
        # We should expect there to be _at least_ one common ancestor, because
        # they should share the same file segment. If that's not the case we
        # we should error because it's likely a bug or programming error.
        assert common_hashes, "DepthInfo comparison shares no common ancestor!"
        common_depth = len(common_hashes)
        return self.stack_hashes[:common_depth]

    def trim(self, amount: int):
        """Return a DepthInfo object with some amount trimmed."""
        if amount == 0:
            # The trivial case.
            return self
        return self.__class__(
            stack_depth=self.stack_depth - amount,
            stack_hashes=self.stack_hashes[:-amount],
            stack_hash_set=self.stack_hash_set.difference(self.stack_hashes[-amount:]),
            stack_class_types=self.stack_class_types[:-amount],
            stack_positions=self.stack_positions[:-amount],
        )


class DepthMap:
    """A mapping of raw segments to depth and parent information.

    This class addresses two needs:
    - To understand configuration of segments with no whitespace
      within them - so the config is related to the parent and
      not the segment)
    - To map the depth of an indent points to apply some precedence
      for where to insert line breaks.

    The internals are structured around a list to do lookups
    and a dict (keyed with the raw segment UUID) to hold the rest.

    """

    def __init__(self, raws_with_stack: Sequence[Tuple[RawSegment, List[PathStep]]]):
        # TODO: decide whether we need the raw segments?
        # self.raw_segments = []
        self.depth_info = {}
        for raw, stack in raws_with_stack:
            # self.raw_segments.append(raw)
            self.depth_info[raw.uuid] = DepthInfo.from_raw_and_stack(raw, stack)

    @classmethod
    def from_parent(cls: Type["DepthMap"], parent: BaseSegment) -> "DepthMap":
        """Generate a DepthMap from all the children of a segment.

        NOTE: This is the most efficient way to construct a DepthMap
        due to caching in the BaseSegment.
        """
        return cls(raws_with_stack=parent.raw_segments_with_ancestors)

    @classmethod
    def from_raws_and_root(
        cls: Type["DepthMap"],
        raw_segments: Sequence[RawSegment],
        root_segment: BaseSegment,
    ) -> "DepthMap":
        """Generate a DepthMap a sequence of raws and a root.

        NOTE: This is the less efficient way to construct a DepthMap
        as it doesn't take advantage of caching in the same way as
        `from_parent`.
        """
        buff = []
        for raw in raw_segments:
            stack = root_segment.path_to(raw)
            buff.append((raw, stack))
        return cls(raws_with_stack=buff)

    def get_depth_info(self, raw: RawSegment) -> DepthInfo:
        """Get the depth info for a given segment."""
        try:
            return self.depth_info[raw.uuid]
        except KeyError as err:  # pragma: no cover
            reflow_logger.exception("Available UUIDS: %s", self.depth_info.keys())
            raise KeyError(
                "Tried to get depth info for unknown "
                f"segment {raw} with UUID {raw.uuid}"
            ) from err

    def copy_depth_info(
        self, anchor: RawSegment, new_segment: RawSegment, trim: int = 0
    ):
        """Copy the depth info for one segment and apply to another.

        This mutates the existing depth map. That's ok because it's
        an idempotent operation and uuids should be unique.

        This is used in edits to a reflow sequence when new segments are
        inserted and can't infer their own depth info.

        NOTE: we don't remove the old one because it causes no harm.
        """
        self.depth_info[new_segment.uuid] = self.get_depth_info(anchor).trim(trim)
