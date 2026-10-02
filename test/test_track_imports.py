"""
Tests for the AST-based static track_imports entrypoint
"""

# Standard
from contextlib import contextmanager
import os
import sys
import tempfile

# Local
from import_tracker.__main__ import main
from import_tracker.import_tracker import format_file_report, track_imports

## Helpers #####################################################################


@contextmanager
def cli_args(*args):
    """Wrapper to set the sys.argv for the enclosed context"""
    prev_argv = sys.argv
    sys.argv = ["dummy_script"] + list(args)
    yield
    sys.argv = prev_argv


@contextmanager
def write_temp_source(contents):
    """Write the given source to a temporary file and yield its path"""
    tmp_dir = tempfile.mkdtemp()
    path = os.path.join(tmp_dir, "sample.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(contents)
    try:
        yield path
    finally:
        os.remove(path)
        os.rmdir(tmp_dir)


def names(entries):
    """The bound names of the given import entries in report order"""
    return [entry.name for entry in entries]


## Scope-local unused tracking ################################################


def test_unused_import_inside_function():
    """An import inside a function body is scoped to that function and only
    counts as used from within that scope
    """
    source = """
import os

def used_at_top():
    return os.getcwd()

def unused_locally():
    import json
    return 1

def used_locally():
    import sys
    return sys.version_info
"""
    report = track_imports(source, is_file=False)
    unused = {entry.name: entry for entry in report.unused}
    assert set(unused.keys()) == {"json"}
    assert unused["json"].scope_depth == 1
    assert "FunctionDef:unused_locally" in unused["json"].scope

    used = {entry.name: entry for entry in report.imports if entry.used}
    assert {"os", "sys"} <= set(used.keys())


def test_unused_import_inside_class_body():
    """Class body imports form their own scope and do not leak into methods"""
    source = """
class Container:
    import csv

    def method(self):
        import json
        return json.dumps({})
"""
    report = track_imports(source, is_file=False)
    unused = names(report.unused)
    assert unused == ["csv"]


def test_top_level_unknown_name_is_still_missing():
    """A missing top-level reference must not be silently hidden"""
    source = """
import os

os.getcwd()
totally_undefined_thing()
"""
    report = track_imports(source, is_file=False)
    missing = {ref.name for ref in report.missing}
    assert missing == {"totally_undefined_thing"}


## TYPE_CHECKING ##############################################################


def test_type_checking_imports_used_in_annotations():
    """Imports behind ``if TYPE_CHECKING`` are annotation-only and therefore
    used, never unused
    """
    source = """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    import decimal


def stamp(when: datetime) -> "datetime":
    return when


value: "decimal.Decimal" = None
"""
    report = track_imports(source, is_file=False)
    assert not report.unused
    entries = {entry.name: entry for entry in report.imports}
    assert entries["datetime"].used
    assert entries["datetime"].type_checking
    assert entries["datetime"].branches == (("if TYPE_CHECKING",),)
    assert entries["decimal"].used
    assert entries["decimal"].type_checking


def test_unused_type_checking_import_is_reported():
    """A TYPE_CHECKING import that never appears in an annotation is unused"""
    source = """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import json
"""
    report = track_imports(source, is_file=False)
    assert names(report.unused) == ["json"]


## Star imports ###############################################################


def test_star_import_attributes_unknown_names_to_source():
    """Unknown names after ``from x import *`` are conservatively attributed
    to x while still being reported as missing references
    """
    source = """
from mystery_pkg import *

thing_one()
thing_two.attr
"""
    report = track_imports(source, is_file=False)
    attributed = {ref.name: ref.attributed_to for ref in report.missing}
    assert attributed == {
        "thing_one": "mystery_pkg",
        "thing_two": "mystery_pkg",
    }
    star = [entry for entry in report.imports if entry.imported == "*"]
    assert len(star) == 1
    assert star[0].module == "mystery_pkg"


def test_star_import_does_not_hide_real_missing_references():
    """Explicitly-bound imports must still resolve, and names that cannot come
    from the star (e.g. function locals) are still reported missing
    """
    source = """
from mystery_pkg import *

def inner():
    return not_defined_anywhere
"""
    report = track_imports(source, is_file=False)
    missing = {ref.name: ref.attributed_to for ref in report.missing}
    assert "not_defined_anywhere" in missing


## Branch merging, ordering and determinism ###################################


def test_duplicate_imports_across_branches_merge():
    """The same name imported in different branches merges into one entry
    with a stable source order and branch-path record
    """
    source = """
import sys

if sys.platform == "win32":
    import json as payload
else:
    import json as payload

payload.dumps({})
"""
    report = track_imports(source, is_file=False)
    payload_entries = [
        entry for entry in report.imports if entry.name == "payload"
    ]
    assert len(payload_entries) == 1
    entry = payload_entries[0]
    assert entry.module == "json"
    assert entry.used
    assert entry.branches == (
        ("else",),
        ("if:sys.platform == 'win32'",),
    )


def test_conflicting_imports_top_level_wins():
    """When the same name is bound unconditionally and inside a branch, the
    top-level binding is the surviving merge entry
    """
    source = """
import json

if some_condition:
    import sys as json

json.dumps({})
"""
    report = track_imports(source, is_file=False)
    entries = [entry for entry in report.imports if entry.name == "json"]
    assert len(entries) == 1
    assert entries[0].module == "json"
    assert entries[0].lineno == 2
    assert entries[0].branches == ()
    assert {ref.name for ref in report.missing} == {"some_condition"}


def test_report_is_deterministic():
    """The same input produces byte-identical reports across runs"""
    source = """
import sys

if sys.platform == "win32":
    import json
else:
    import json

def f():
    import os
    return os.getcwd()

json.dumps({})
"""
    first = format_file_report(source, is_file=False)
    second = format_file_report(source, is_file=False)
    assert first == second


def test_report_order_is_by_source_position():
    """Imports are emitted in source order even across branches and scopes"""
    source = """
import bbb
import aaa

def f():
    import ccc
"""
    report = track_imports(source, is_file=False)
    assert names(report.imports) == ["bbb", "aaa", "ccc"]


## File mode ##################################################################


def test_main_file_mode_prints_indented_listing(capsys):
    """The __main__ entrypoint prints an indented attribution listing for a
    single source file
    """
    source = """
import os

if TYPE_CHECKING:
    import json


def f():
    import sys
"""
    with write_temp_source(source) as path:
        with cli_args("--file", path):
            main()
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert "scope: <module> (depth 0)" in lines
    assert any(line.strip() == "import os  # UNUSED" for line in lines)
    assert any(line.startswith("  ") and "FunctionDef:f" not in line for line in lines)
    function_lines = [line for line in lines if "import sys" in line]
    assert function_lines
    assert function_lines[0].startswith("    ")


def test_main_file_mode_tracks_real_file(capsys):
    """The file mode works when pointed at an existing file path"""
    here = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(here, "..", "import_tracker", "import_tracker.py")
    with cli_args("--file", os.path.abspath(target)):
        main()
    captured = capsys.readouterr()
    assert "scope: <module> (depth 0)" in captured.out
    assert "import ast" in captured.out
