"""
Tests for the AST-based single-file import tracker (track_imports)
"""

# Standard
from contextlib import contextmanager
import json
import sys

# Local
from import_tracker.__main__ import main
from import_tracker.import_tracker import format_import_report, track_imports

## Helpers #####################################################################


@contextmanager
def cli_args(*args):
    """Wrapper to set the sys.argv for the enclosed context"""
    prev_argv = sys.argv
    sys.argv = ["dummy_script"] + list(args)
    yield
    sys.argv = prev_argv


def imports_by_symbol(report, symbol):
    """Find all import entries for a symbol in a report"""
    return [entry for entry in report["imports"] if entry["symbol"] == symbol]


## Tests #######################################################################


def test_function_scoped_unused_import():
    """An import inside a function body lives in the function's scope, stays
    out of the module-level symbol table, and is reported unused when nothing
    in its own scope references it
    """
    source = (
        "import os\n"
        "\n"
        "def helper():\n"
        "    import requests\n"
        "    return os.getcwd()\n"
    )
    report = track_imports(source, filename="func_unused.py")

    # The module-level import is used from within the function
    (os_entry,) = imports_by_symbol(report, "os")
    assert os_entry["scope"] == ""
    assert os_entry["depth"] == 0
    assert os_entry["used"] is True

    # The function-level import is scoped to the function and unused
    (requests_entry,) = imports_by_symbol(report, "requests")
    assert requests_entry["scope"] == "helper"
    assert requests_entry["depth"] == 1
    assert requests_entry["used"] is False
    assert [entry["symbol"] for entry in report["unused"]] == ["requests"]
    assert report["unresolved"] == []


def test_type_checking_imports():
    """Imports under `if TYPE_CHECKING:` live in their own scope; forward
    references in annotations always count as used while unreferenced ones
    are reported unused
    """
    source = (
        "from typing import TYPE_CHECKING, List\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from collections import OrderedDict\n"
        "    import pathlib\n"
        "\n"
        "def get_mapping() -> 'OrderedDict':\n"
        "    ...\n"
        "\n"
        "def get_names() -> List['OrderedDict']:\n"
        "    ...\n"
    )
    report = track_imports(source, filename="type_checking.py")

    (ordered_dict_entry,) = imports_by_symbol(report, "OrderedDict")
    assert ordered_dict_entry["type_checking"] is True
    assert ordered_dict_entry["scope"] == "TYPE_CHECKING"
    # Used only via (string) forward references in annotations
    assert ordered_dict_entry["used"] is True

    (pathlib_entry,) = imports_by_symbol(report, "pathlib")
    assert pathlib_entry["type_checking"] is True
    assert pathlib_entry["used"] is False

    # TYPE_CHECKING itself is used by the `if` statement and List by the
    # annotation, so only pathlib is unused and nothing is unresolved
    assert [entry["symbol"] for entry in report["unused"]] == ["pathlib"]
    assert report["unresolved"] == []


def test_star_import_attribution():
    """Names that cannot be attributed to anything else are conservatively
    attributed to a visible `from x import *`, while genuinely missing
    references outside the star's visibility are still reported
    """
    source = (
        "from mymod import *\n"
        "\n"
        "def run():\n"
        "    return foo() + bar\n"
    )
    report = track_imports(source, filename="star.py")

    (star_entry,) = imports_by_symbol(report, "*")
    assert star_entry["module"] == "mymod"
    assert star_entry["star"] is True
    assert star_entry["used"] is True
    # Unknown names absorbed by the star import, none left unresolved
    assert report["star_attributions"] == {"bar": "mymod", "foo": "mymod"}
    assert report["unresolved"] == []
    assert report["unused"] == []

    # A star import inside a function must not mask genuinely missing
    # references at module level
    source = (
        "def run():\n"
        "    from mymod import *\n"
        "    return foo()\n"
        "\n"
        "missing_call()\n"
    )
    report = track_imports(source, filename="star_missing.py")
    assert report["star_attributions"] == {"foo": "mymod"}
    assert report["unresolved"] == ["missing_call"]


def test_branch_imports_merged():
    """The same symbol imported in different branches merges into one entry"""
    source = (
        "try:\n"
        "    import ujson as json\n"
        "except ImportError:\n"
        "    import json\n"
        "\n"
        "json.dumps({})\n"
    )
    report = track_imports(source, filename="branches.py")
    entries = imports_by_symbol(report, "json")
    assert len(entries) == 1
    (entry,) = entries
    assert entry["used"] is True
    assert entry["branches"] == ["try@1:body", "try@1:handler0"]
    assert report["unused"] == []


def test_conflicting_imports_top_level_wins():
    """When a symbol is imported both in a branch and at the top level, the
    top-level import wins the conflict and the entries still merge
    """
    source = (
        "if True:\n"
        "    import ujson as json\n"
        "\n"
        "import json\n"
        "\n"
        "json.dumps({})\n"
    )
    report = track_imports(source, filename="conflict.py")
    entries = imports_by_symbol(report, "json")
    assert len(entries) == 1
    (entry,) = entries
    assert entry["module"] == "json"
    assert entry["alias"] is None
    assert entry["branches"] == ["if@1:body"]
    assert entry["used"] is True


def test_report_is_deterministic():
    """The same input must produce byte-identical reports on repeated runs"""
    source = (
        "import os\n"
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    import pathlib\n"
        "\n"
        "try:\n"
        "    import ujson as json\n"
        "except ImportError:\n"
        "    import json\n"
        "\n"
        "from mymod import *\n"
        "\n"
        "def helper() -> 'pathlib.Path':\n"
        "    import requests\n"
        "    return json.dumps({'cwd': os.getcwd(), 'ext': foo()})\n"
    )
    first = json.dumps(track_imports(source, filename="det.py"), indent=2)
    second = json.dumps(track_imports(source, filename="det.py"), indent=2)
    assert first == second


def test_main_file_mode(capsys, tmp_path):
    """The __main__ entrypoint prints an indented attribution listing for a
    single file
    """
    target = tmp_path / "sample.py"
    target.write_text(
        "import os\n"
        "\n"
        "def helper():\n"
        "    import requests\n"
        "    return os.getcwd()\n",
        encoding="utf-8",
    )
    with cli_args("--file", str(target)):
        main()
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0] == "<module>:"
    assert "  os -> os [used]" in lines
    assert "  helper:" in lines
    assert "    requests -> requests [unused]" in lines
