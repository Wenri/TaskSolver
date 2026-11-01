from collections.abc import Iterator
from .sections import Section, Symbol
from .segments import Segment
from .relocation import RelocationTable

class DynamicTagEntry:
    d_tag: str | int
    d_val: int
    d_ptr: int

class DynamicTag:
    entry: DynamicTagEntry

class Dynamic:
    def iter_tags(self, type: str | None = None) -> Iterator[DynamicTag]: ...
    def get_relocation_tables(self) -> dict[str, RelocationTable]: ...
    def get_symbol(self, index: int) -> Symbol: ...

class DynamicSegment(Segment, Dynamic): ...
class DynamicSection(Section, Dynamic): ...
