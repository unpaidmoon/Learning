"""Original terminal aggregation plus a project-side precondition wrapper."""

from .book_models import BlockStatus, Page, PageStatus


class _OriginalCompilerState:
    @staticmethod
    def _finalize_page_status(page: Page) -> None:
        if not page.blocks:
            page.status = PageStatus.ERROR
            page.error = "No blocks were planned for this page."
            return

        ready = sum(1 for b in page.blocks if b.status == BlockStatus.READY)
        errored = sum(1 for b in page.blocks if b.status == BlockStatus.ERROR)
        if ready == len(page.blocks):
            page.status = PageStatus.READY
            page.error = ""
        elif ready == 0:
            page.status = PageStatus.ERROR
            page.error = f"All {errored} blocks failed to generate."
        else:
            page.status = PageStatus.PARTIAL
            page.error = f"{errored}/{len(page.blocks)} blocks failed."



def finalize_terminal_page(page: Page) -> None:
    """Aggregate only READY/ERROR blocks; other states still need a decision."""
    if any(block.status not in {BlockStatus.READY, BlockStatus.ERROR} for block in page.blocks):
        raise ValueError("PAGE_NOT_TERMINAL")
    _OriginalCompilerState._finalize_page_status(page)
