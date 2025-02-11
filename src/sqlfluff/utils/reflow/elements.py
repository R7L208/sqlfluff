"""Dataclasses for reflow work."""

from itertools import chain
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple, Type, cast

from sqlfluff.core.parser import BaseSegment, RawSegment
from sqlfluff.core.parser.segments.raw import WhitespaceSegment
from sqlfluff.core.rules.base import LintFix

from sqlfluff.utils.reflow.config import ReflowConfig
from sqlfluff.utils.reflow.depthmap import DepthInfo

# We're in the utils module, but users will expect reflow
# logs to appear in the context of rules. Hence it's a subset
# of the rules logger.
reflow_logger = logging.getLogger("sqlfluff.rules.reflow")


@dataclass(frozen=True)
class ReflowElement:
    """Base reflow element class."""

    segments: Sequence[RawSegment]

    @staticmethod
    def _class_types(segments: Sequence[RawSegment]) -> Set[str]:
        return set(chain.from_iterable(seg.class_types for seg in segments))

    @property
    def raw(self):
        """Get the current raw representation."""
        return "".join(seg.raw for seg in self.segments)


@dataclass(frozen=True)
class ReflowBlock(ReflowElement):
    """Class for keeping track of elements to reflow.

    It holds segments to reflow and also exposes configuration
    around how they are expected to reflow around others.

    The attributes exposed are designed to be "post configuration"
    i.e. they should reflect configuration appropriately.

    NOTE: These are the smallest unit of "work" within
    the reflow methods, and may contain meta segments.
    """

    # Options for spacing rules are:
    # - single:         the default (one single space)
    # - touch:          no whitespace
    # - any:            don't enforce any spacing rules
    spacing_before: str
    spacing_after: str
    # The depth info is used in determining where to put line breaks.
    depth_info: DepthInfo
    # This stores relevant configs for segments in the stack.
    stack_spacing_configs: Dict[int, str]

    @classmethod
    def from_config(
        cls: Type["ReflowBlock"], segments, config: ReflowConfig, depth_info: DepthInfo
    ) -> "ReflowBlock":
        """Extendable constructor which accepts config."""
        block_config = config.get_block_config(cls._class_types(segments), depth_info)
        # Populate any spacing_within config.
        # TODO: This needs decent unit tests - not just what happens in rules.
        stack_spacing_configs = {}
        for hash, class_types in zip(
            depth_info.stack_hashes, depth_info.stack_class_types
        ):
            spacing_within = config.get_block_config(class_types).spacing_within
            if spacing_within:
                stack_spacing_configs[hash] = spacing_within
        return cls(
            segments=segments,
            spacing_before=block_config.spacing_before,
            spacing_after=block_config.spacing_after,
            depth_info=depth_info,
            stack_spacing_configs=stack_spacing_configs,
        )


@dataclass(frozen=True)
class ReflowPoint(ReflowElement):
    """Class for keeping track of editable elements in reflow.

    It holds segments which can be changed during a reflow operation
    such as whitespace and newlines.

    It holds no configuration and is influenced by the blocks either
    side.
    """

    @staticmethod
    def _determine_constraints(
        prev_block: Optional[ReflowBlock],
        next_block: Optional[ReflowBlock],
        strip_newlines: bool = False,
    ) -> Tuple[str, str, bool]:
        """Given the surrounding blocks, determine appropriate constraints."""
        # Start with the defaults.
        pre_constraint = prev_block.spacing_after if prev_block else "single"
        post_constraint = next_block.spacing_before if next_block else "single"

        # Work out the common parent segment and depth
        if prev_block and next_block:
            common = prev_block.depth_info.common_with(next_block.depth_info)
            # Just check the most immediate parent for now for speed.
            # TODO: Review whether just checking the parent is enough.
            # NOTE: spacing configs will be available on both sides if they're common
            # so it doesn't matter whether we get it from prev_block or next_block.
            within_constraint = prev_block.stack_spacing_configs.get(common[-1], None)
            if not within_constraint:
                pass
            elif within_constraint in ("touch", "inline"):
                # NOTE: inline is actually a more extreme version of "touch".
                # Examples:
                # - "inline" would be used with an object reference, where the
                #   parts have to all be together on one line like `a.b.c`.
                # - "touch" would allow the above layout, _but also_ allow an
                #   an optional line break between, much like between an opening
                #   bracket and the following element: `(a)` or:
                #   ```
                #   (
                #       a
                #   )
                #   ```
                if within_constraint == "inline":
                    # If they are then strip newlines.
                    strip_newlines = True
                # If segments are expected to be touch within. Then modify
                # constraints accordingly.
                # NOTE: We don't override if it's already "any"
                if pre_constraint != "any":
                    pre_constraint = "touch"
                if post_constraint != "any":
                    post_constraint = "touch"
            else:  # pragma: no cover
                idx = prev_block.depth_info.stack_hashes.index(common[-1])
                raise NotImplementedError(
                    f"Unexpected within constraint: {within_constraint} for "
                    f"{prev_block.depth_info.stack_class_types[idx]}"
                )

        return pre_constraint, post_constraint, strip_newlines

    @staticmethod
    def _process_spacing(
        segment_buffer: List[RawSegment], strip_newlines: bool = False
    ) -> Tuple[List[RawSegment], List[RawSegment], List[LintFix]]:
        """Given the existing spacing, extract information and do basic pruning."""
        removal_buffer: List[RawSegment] = []
        last_whitespace: List[RawSegment] = []

        # Loop through the existing segments looking for spacing.
        for seg in segment_buffer:

            # If it's whitespace, store it.
            if seg.is_type("whitespace"):
                last_whitespace.append(seg)

            # If it's a newline, react accordingly.
            elif seg.is_type("newline", "end_of_file"):

                # Are we stripping newlines?
                if strip_newlines and seg.is_type("newline"):
                    reflow_logger.debug("    Stripping newline: %s", seg)
                    removal_buffer.append(seg)
                    # Carry on as though it wasn't here.
                    continue

                # Check if we've just passed whitespace. If we have, remove it
                # as trailing whitespace, both from the buffer and create a fix.
                if last_whitespace:
                    reflow_logger.debug("    Removing trailing whitespace.")
                    for ws in last_whitespace:
                        removal_buffer.append(ws)

                # Regardless, unset last_whitespace.
                # We either just deleted it, or it's not relevant for any future
                # segments.
                last_whitespace = []

        if len(last_whitespace) >= 2:
            reflow_logger.debug("   Removing adjoining whitespace.")
            # If we find multiple sequential whitespaces, it's the sign
            # that we've removed something. Only the first one should be
            # a valid indent (or the one we consider for constraints).
            # Remove all the following ones.
            for ws in last_whitespace[1:]:
                removal_buffer.append(ws)

        # Turn the removal buffer updated segment buffer, last whitespace
        # and associated fixes.
        return (
            [s for s in segment_buffer if s not in removal_buffer],
            last_whitespace,
            [LintFix.delete(s) for s in removal_buffer],
        )

    @staticmethod
    def _determine_aligned_inline_spacing(
        root_segment: BaseSegment,
        whitespace_seg: RawSegment,
        next_seg: RawSegment,
        segment_type: str,
        align_within: Optional[str],
        align_boundary: Optional[str],
    ) -> str:
        """Work out spacing for instance of an `align` constraint."""
        # Find the level of segment that we're aligning.
        # NOTE: Reverse slice
        parent_segment = None
        for ps in root_segment.path_to(next_seg)[::-1]:
            if ps.segment.is_type(align_within):
                parent_segment = ps.segment
            if ps.segment.is_type(align_boundary):
                break

        if not parent_segment:
            reflow_logger.debug(
                "    No Parent found for alignment case. Treat as single."
            )
            return " "

        # We've got a parent. Find some siblings.
        reflow_logger.debug("    Determining alignment within: %s", parent_segment)
        siblings = []
        for sibling in parent_segment.recursive_crawl(segment_type):
            # Purge any siblings with a boundary between them
            if not any(
                ps.segment.is_type(align_boundary)
                for ps in parent_segment.path_to(sibling)
            ):
                siblings.append(sibling)
            else:
                reflow_logger.debug(
                    "    Purging a sibling because they're blocked "
                    "by a boundary: %s",
                    sibling,
                )

        # Is the current indent the only one on the line?
        if any(
            # Same line
            sibling.pos_marker.working_line_no == next_seg.pos_marker.working_line_no
            # And not same position (i.e. not self)
            and sibling.pos_marker.working_line_pos
            != next_seg.pos_marker.working_line_pos
            for sibling in siblings
        ):
            reflow_logger.debug("    Found sibling on same line. Treat as single")
            return " "

        # Work out the current spacing before each.
        last_code = None
        max_desired_line_pos = 0
        for seg in parent_segment.raw_segments:
            for sibling in siblings:
                # NOTE: We're asserting that there must have been
                # a last_code. Otherwise this won't work.
                if (
                    seg.pos_marker.working_loc == sibling.pos_marker.working_loc
                    and last_code
                ):
                    loc = last_code.pos_marker.working_loc_after(last_code.raw)
                    reflow_logger.debug(
                        "    loc for %s: %s from %s",
                        sibling,
                        loc,
                        last_code,
                    )
                    if loc[1] > max_desired_line_pos:
                        max_desired_line_pos = loc[1]
            if seg.is_code:
                last_code = seg

        desired_space = " " * (
            1 + max_desired_line_pos - whitespace_seg.pos_marker.working_line_pos
        )
        reflow_logger.debug(
            "    desired_space: %r (based on max line pos of %s)",
            desired_space,
            max_desired_line_pos,
        )
        return desired_space

    @classmethod
    def _handle_respace__inline_with_space(
        cls,
        pre_constraint: str,
        post_constraint: str,
        next_block: Optional[ReflowBlock],
        root_segment: BaseSegment,
        segment_buffer: List[RawSegment],
        last_whitespace: List[RawSegment],
    ) -> Tuple[List[RawSegment], List[LintFix]]:
        """Check inline spacing is the right size.

        This forms one of the cases handled by .respace_point().

        This code assumes:
        - a ReflowPoint with no newlines.
        - a ReflowPoint which has _some_ whitespace.

        Given this we apply constraints to ensure the whitespace
        is of an appropriate size.
        """
        new_fixes: List[LintFix] = []
        # Get some indices so that we can reference around them
        ws_seg = last_whitespace[0]
        ws_idx = segment_buffer.index(ws_seg)

        # Do we have either side set to "any"
        if "any" in [pre_constraint, post_constraint]:
            # In this instance - don't change anything.
            # e.g. this could mean there is a comment on one side.
            return segment_buffer, new_fixes

        # Do we have either side set to "touch"?
        if "touch" in [pre_constraint, post_constraint]:
            # In this instance - no whitespace is correct, This
            # means we should delete it.
            new_fixes.append(
                LintFix(
                    "delete",
                    anchor=ws_seg,
                )
            )
            segment_buffer.pop(ws_idx)
            return segment_buffer, new_fixes

        # Handle left alignment & singles
        if (
            post_constraint.startswith("align") and next_block
        ) or pre_constraint == post_constraint == "single":

            # Determine the desired spacing, either as alignment or as a single.
            if post_constraint.startswith("align") and next_block:
                alignment_config = post_constraint.split(":")
                seg_type = alignment_config[1]
                align_within = (
                    alignment_config[2] if len(alignment_config) > 2 else None
                )
                align_boundary = (
                    alignment_config[3] if len(alignment_config) > 3 else None
                )
                reflow_logger.debug(
                    "    Alignment Config: %s, %s, %s, %s",
                    seg_type,
                    align_within,
                    align_boundary,
                    next_block.segments[0].pos_marker.working_line_pos,
                )

                desired_space = cls._determine_aligned_inline_spacing(
                    root_segment,
                    ws_seg,
                    next_block.segments[0],
                    seg_type,
                    align_within,
                    align_boundary,
                )
            else:
                desired_space = " "

            if ws_seg.raw != desired_space:
                new_seg = ws_seg.edit(desired_space)
                new_fixes.append(
                    LintFix(
                        "replace",
                        anchor=ws_seg,
                        edit=[new_seg],
                    )
                )
                segment_buffer[ws_idx] = new_seg

            return segment_buffer, new_fixes

        raise NotImplementedError(  # pragma: no cover
            f"Unexpected Constraints: {pre_constraint}, {post_constraint}"
        )

    @staticmethod
    def _handle_respace__inline_without_space(
        pre_constraint: str,
        post_constraint: str,
        prev_block: Optional[ReflowBlock],
        next_block: Optional[ReflowBlock],
        segment_buffer: List[RawSegment],
        existing_fixes: List[LintFix],
    ) -> Tuple[List[RawSegment], List[LintFix], bool]:
        """Ensure spacing is the right size.

        This forms one of the cases handled by .respace_point().

        This code assumes:
        - a ReflowPoint with no newlines.
        - a ReflowPoint which _no_ whitespace.

        Given this we apply constraints to either confirm no
        spacing is required or create some of the right size.
        """
        edited = False
        new_fixes: List[LintFix] = []
        # Do we have either side set to "touch" or "any"
        if {"touch", "any"}.intersection([pre_constraint, post_constraint]):
            # In this instance - no whitespace is correct.
            # Either because there shouldn't be, or because "any"
            # means we shouldn't check.
            pass
        # Handle the default case
        elif pre_constraint == post_constraint == "single":
            # Insert a single whitespace.
            reflow_logger.debug("    Inserting Single Whitespace.")
            # Add it to the buffer first (the easy bit). The hard bit
            # is to then determine how to generate the appropriate LintFix
            # objects.
            segment_buffer.append(WhitespaceSegment())
            edited = True

            # So special handling here. If segments either side
            # already exist then we don't care which we anchor on
            # but if one is already an insertion (as shown by a lack)
            # of pos_marker, then we should piggy back on that pre-existing
            # fix.
            existing_fix = None
            insertion = None
            if prev_block and not prev_block.segments[-1].pos_marker:
                existing_fix = "after"
                insertion = prev_block.segments[-1]
            elif next_block and not next_block.segments[0].pos_marker:
                existing_fix = "before"
                insertion = next_block.segments[0]

            if existing_fix:
                reflow_logger.debug("    Detected existing fix %s", existing_fix)
                if not existing_fixes:  # pragma: no cover
                    raise ValueError(
                        "Fixes detected, but none passed to .respace(). "
                        "This will cause conflicts."
                    )
                # Find the fix
                for fix in existing_fixes:
                    # Does it contain the insertion?
                    # TODO: This feels ugly - eq for BaseSegment is different
                    # to uuid matching for RawSegment. Perhaps this should be
                    # more aligned. There might be a better way of doing this.
                    if (
                        insertion
                        and fix.edit
                        and insertion.uuid in [elem.uuid for elem in fix.edit]
                    ):
                        break
                else:  # pragma: no cover
                    reflow_logger.warning("Fixes %s", existing_fixes)
                    raise ValueError(f"Couldn't find insertion for {insertion}")
                # Mutate the existing fix
                assert fix
                assert fix.edit  # It's going to be an edit if we've picked it up.
                if existing_fix == "before":
                    fix.edit = [cast(BaseSegment, WhitespaceSegment())] + fix.edit
                elif existing_fix == "after":
                    fix.edit = fix.edit + [cast(BaseSegment, WhitespaceSegment())]
            else:
                reflow_logger.debug("    Not Detected existing fix. Creating new")
                if prev_block:
                    new_fixes.append(
                        LintFix(
                            "create_after",
                            anchor=prev_block.segments[-1],
                            edit=[WhitespaceSegment()],
                        )
                    )
                # TODO: We should have a test which covers this clause.
                elif next_block:  # pragma: no cover
                    new_fixes.append(
                        LintFix(
                            "create_before",
                            anchor=next_block.segments[0],
                            edit=[WhitespaceSegment()],
                        )
                    )
                else:  # pragma: no cover
                    NotImplementedError(
                        "Not set up to handle a missing _after_ and _before_."
                    )
        else:  # pragma: no cover
            # TODO: This will get test coverage when configuration routines
            # are in properly.
            raise NotImplementedError(
                f"Unexpected Constraints: {pre_constraint}, {post_constraint}"
            )

        return segment_buffer, existing_fixes + new_fixes, edited

    def respace_point(
        self,
        prev_block: Optional[ReflowBlock],
        next_block: Optional[ReflowBlock],
        root_segment: BaseSegment,
        fixes: List[LintFix],
        strip_newlines: bool = False,
    ) -> Tuple[List[LintFix], "ReflowPoint"]:
        """Respace a point based on given constraints.

        NB: This effectively includes trailing whitespace fixes.

        Deletion and edit fixes are generated immediately, but creations
        are paused to the end and done in bulk so as not to generate conflicts.

        Note that the `strip_newlines` functionality exists here as a slight
        exception to pure respacing, but as a very simple case of positioning
        line breaks. The default operation of `respace` does not enable it
        however it exists as a convenience for rules which wish to use it.
        """
        pre_constraint, post_constraint, strip_newlines = self._determine_constraints(
            prev_block, next_block, strip_newlines
        )

        reflow_logger.debug("Respacing: %s", self)

        # The buffer is used to create the new reflow point to return
        segment_buffer, last_whitespace, new_fixes = self._process_spacing(
            list(self.segments), strip_newlines
        )

        # Is there a newline?
        # NOTE: We do this based on the segment buffer rather than self.class_types
        # because we may have just removed any present newlines in the buffer.
        if any(seg.is_type("newline", "end_of_file") for seg in segment_buffer):
            # Most of this section should be handled as _Indentation_.
            # BUT: There is one case we should handle here.
            # If we find that the last whitespace has a newline
            # before it, and the position markers imply there was
            # a removal between them, then remove the whitespace.
            # This ensures a consistent indent.
            # TODO: Check this doesn't duplicate indentation code
            # once written.

            # The test is less about whether it's longer than one
            # (because we should already have removed additional
            # whitespace above). This is about attempting consistency.
            if len(last_whitespace) == 1:
                ws_seg = last_whitespace[0]
                ws_idx = self.segments.index(ws_seg)
                if ws_idx > 0:
                    prev_seg = self.segments[ws_idx - 1]
                    if (
                        prev_seg.is_type("newline")
                        # Not just unequal. Must be actively _before_.
                        # NOTE: Based on working locations
                        and prev_seg.get_end_loc() < ws_seg.get_start_loc()
                    ):
                        reflow_logger.debug(
                            "    Removing non-contiguous whitespace post removal."
                        )
                        segment_buffer.remove(ws_seg)
                        new_fixes.append(LintFix("delete", ws_seg))

        # Is this an inline case? (i.e. no newline)
        else:
            reflow_logger.debug(
                "    Inline case. Constraints: %s <-> %s.",
                pre_constraint,
                post_constraint,
            )

            # Do we at least have _some_ whitespace?
            if last_whitespace:
                # We do - is it the right size?
                segment_buffer, delta_fixes = self._handle_respace__inline_with_space(
                    pre_constraint,
                    post_constraint,
                    next_block,
                    root_segment,
                    segment_buffer,
                    last_whitespace,
                )
                new_fixes.extend(delta_fixes)
            else:
                # No. Should we insert some?
                # NOTE: This method operates on the existing fix buffer.
                (
                    segment_buffer,
                    fixes,
                    edited,
                ) = self._handle_respace__inline_without_space(
                    pre_constraint,
                    post_constraint,
                    prev_block,
                    next_block,
                    segment_buffer,
                    fixes,
                )
                if edited:
                    reflow_logger.debug("    Modified fix buffer: %s", fixes)

        # Only log if we actually made a change.
        if new_fixes:
            reflow_logger.debug("    New Fixes: %s", new_fixes)

        return fixes + new_fixes, ReflowPoint(segment_buffer)
