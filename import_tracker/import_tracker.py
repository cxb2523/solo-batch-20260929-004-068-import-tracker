"""
This module implements utilities that enable tracking of third party deps
through import statements
"""
# Standard
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union
from dataclasses import dataclass, field
import ast
import dis
import importlib
import os
import re
import sys
import tokenize

# Local
from . import constants
from .log import log

## Public ######################################################################


@dataclass(frozen=True)
class ImportEntry:
    """A single merged import binding (one local name -> one source module)

    Multiple ``import`` statements binding the same name within the same scope
    (e.g. across the branches of an ``if``) are merged into one entry.
    """

    name: str
    module: str
    imported: Optional[str]
    lineno: int
    col: int
    scope_depth: int
    scope: str
    branches: Tuple[Tuple[str, ...], ...]
    type_checking: bool
    used: bool

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a json-serializable dict with a stable key order"""
        return {
            "name": self.name,
            "module": self.module,
            "imported": self.imported,
            "lineno": self.lineno,
            "col": self.col,
            "scope_depth": self.scope_depth,
            "scope": self.scope,
            "branches": [list(path) for path in self.branches],
            "type_checking": self.type_checking,
            "used": self.used,
        }


@dataclass(frozen=True)
class MissingRef:
    """A referenced name that could not be resolved to any binding"""

    name: str
    lineno: int
    col: int
    scope_depth: int
    scope: str
    attributed_to: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a json-serializable dict with a stable key order"""
        return {
            "name": self.name,
            "lineno": self.lineno,
            "col": self.col,
            "scope_depth": self.scope_depth,
            "scope": self.scope,
            "attributed_to": self.attributed_to,
        }


@dataclass(frozen=True)
class ImportReport:
    """The full result of :func:`track_imports` for one source file"""

    imports: Tuple[ImportEntry, ...]
    missing: Tuple[MissingRef, ...]

    @property
    def unused(self) -> Tuple[ImportEntry, ...]:
        """All merged import entries that were never referenced"""
        return tuple(entry for entry in self.imports if not entry.used)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a json-serializable dict with a stable key order"""
        return {
            "imports": [entry.to_dict() for entry in self.imports],
            "unused": [entry.to_dict() for entry in self.unused],
            "missing": [missing.to_dict() for missing in self.missing],
        }


def track_imports(
    source: Union[str, "os.PathLike[str]"],
    *,
    is_file: Optional[bool] = None,
) -> ImportReport:
    """Statically track the imports of a single python file via its AST

    The AST is traversed exactly once. Each import records the lexical scope
    depth and conditional branch path in which it appears. Usage is resolved
    per lexical scope, so imports inside function bodies, class bodies and
    ``if TYPE_CHECKING`` blocks never leak into the module-level symbol table.

    Args:
        source:  Union[str, os.PathLike]
            Either python source code or the path to a python file.
        is_file:  Optional[bool]
            Force ``source`` to be interpreted as a path (True) or as code
            (False). If None, existing paths are treated as files.

    Returns:
        ImportReport
    """
    filename, src = _read_source(source, is_file)
    tree = ast.parse(src, filename=filename or "<source>")
    visitor = _ImportTrackingVisitor()
    visitor.visit(tree)
    return visitor.build_report()


def format_file_report(
    source: Union[str, "os.PathLike[str]"],
    *,
    is_file: Optional[bool] = None,
    indent: int = 2,
) -> str:
    """Render a human-readable, indented attribution listing for one file"""
    report = track_imports(source, is_file=is_file)
    return _render_report(report, indent)


def track_module(
    module_name: str,
    package_name: Optional[str] = None,
    submodules: Union[List[str], bool] = False,
    track_import_stack: bool = False,
    full_depth: bool = False,
    detect_transitive: bool = False,
    show_optional: bool = False,
) -> Union[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Track the dependencies of a single python module

    Args:
        module_name:  str
            The name of the module to track (may be relative if package_name
            provided)
        package_name:  Optional[str]
            The parent package name of the module if the module name is relative
        submodules:  Union[List[str], bool]
            If True, all submodules of the given module will also be tracked. If
            given as a list of strings, only those submodules will be tracked.
            If False, only the named module will be tracked.
        track_import_stack:  bool
            Store the stacks of modules causing each dependency of each tracked
            module for debugging purposes.
        full_depth:  bool
            Include transitive dependencies of the third party dependencies that
            are direct dependencies of modules within the target module's parent
            library.
        detect_transitive:  bool
            Detect whether each dependency is 'direct' or 'transitive'
        show_optional:  bool
            Show whether each requirement is optional (behind a try/except) or
            not

    Returns:
        import_mapping:  Union[Dict[str, List[str]], Dict[str, Dict[str, Any]]]
            The mapping from fully-qualified module name to the set of imports
            needed by the given module. If tracking import stacks or detecting
            direct vs transitive dependencies, the output schema is
            Dict[str, Dict[str, Any]] where the nested dicts hold "stack" and/or
            "type" keys respectively. If neither feature is enabled, the schema
            is Dict[str, List[str]].
    """

    # Import the target module
    log.debug("Importing %s.%s", package_name, module_name)
    imported = importlib.import_module(module_name, package=package_name)
    full_module_name = imported.__name__

    # Recursively build the mapping
    module_deps_map = dict()
    modules_to_check = {imported}
    checked_modules = set()
    tracked_module_root_pkg = full_module_name.partition(".")[0]
    while modules_to_check:
        next_modules_to_check = set()
        for module_to_check in modules_to_check:

            # Figure out all direct imports from this module
            req_imports, opt_imports = _get_imports(module_to_check)
            opt_dep_names = {mod.__name__ for mod in opt_imports}
            all_imports = req_imports.union(opt_imports)
            module_import_names = {mod.__name__ for mod in all_imports}
            log.debug3(
                "Full import names for [%s]: %s",
                module_to_check.__name__,
                module_import_names,
            )

            # Trim to just non-standard modules
            non_std_module_names = _get_non_std_modules(module_import_names)
            log.debug3("Non std module names: %s", non_std_module_names)
            non_std_module_imports = [
                mod for mod in all_imports if mod.__name__ in non_std_module_names
            ]

            # Set the deps for this module as a mapping from each dep to its
            # optional status
            module_deps_map[module_to_check.__name__] = {
                mod: mod in opt_dep_names for mod in non_std_module_names
            }
            log.debug2(
                "Deps for [%s] -> %s",
                module_to_check.__name__,
                non_std_module_names,
            )

            # Add each of these modules to the next round of modules to check if
            # it has not yet been checked
            next_modules_to_check = next_modules_to_check.union(
                {
                    mod
                    for mod in non_std_module_imports
                    if (
                        mod not in checked_modules
                        and (
                            full_depth
                            or mod.__name__.partition(".")[0] == tracked_module_root_pkg
                        )
                    )
                }
            )

            # Also check modules with intermediate names
            parent_mods = set()
            for mod in next_modules_to_check:
                mod_name_parts = mod.__name__.split(".")
                for parent_mod_name in [
                    ".".join(mod_name_parts[: i + 1])
                    for i in range(len(mod_name_parts))
                ]:
                    parent_mod = sys.modules.get(parent_mod_name)
                    if parent_mod is None:
                        log.warning(
                            "Could not find parent module %s of %s",
                            parent_mod_name,
                            mod.__name__,
                        )
                        continue
                    if parent_mod not in checked_modules:
                        parent_mods.add(parent_mod)
            next_modules_to_check = next_modules_to_check.union(parent_mods)

            # Mark this module as checked
            checked_modules.add(module_to_check)

        # Set the next iteration
        log.debug3("Next modules to check: %s", next_modules_to_check)
        modules_to_check = next_modules_to_check

    log.debug3("Full module dep mapping: %s", module_deps_map)

    # Determine all the modules we want the final answer for
    output_mods = {full_module_name}
    if submodules:
        output_mods = output_mods.union(
            {
                mod
                for mod in module_deps_map
                if (
                    (submodules is True and mod.startswith(full_module_name))
                    or (submodules is not True and mod in submodules)
                )
            }
        )
    log.debug2("Output modules: %s", output_mods)

    # Add parent direct deps to the module deps map
    parent_direct_deps = _find_parent_direct_deps(module_deps_map)

    # Flatten each of the output mods' dependency lists
    flattened_deps = {
        mod: _flatten_deps(mod, module_deps_map, parent_direct_deps)
        for mod in output_mods
    }
    log.debug("Raw output deps map: %s", flattened_deps)

    # If not displaying any of the extra info, the values are simple lists of
    # dependency names
    if not any([detect_transitive, track_import_stack, show_optional]):
        deps_out = {
            mod: list(sorted(deps.keys())) for mod, (deps, _) in flattened_deps.items()
        }

    # Otherwise, the values will be dicts with some combination of "type" and
    # "stack" populated
    else:
        deps_out = {mod: {} for mod in flattened_deps.keys()}

    # If detecting transitive deps, look through the stacks and mark each dep as
    # transitive or direct
    if detect_transitive:
        for mod, (deps, _) in flattened_deps.items():
            for dep_name, dep_stacks in deps.items():
                deps_out.setdefault(mod, {}).setdefault(dep_name, {})[
                    constants.INFO_TYPE
                ] = (
                    constants.TYPE_DIRECT
                    if any(len(dep_stack) == 1 for dep_stack in dep_stacks)
                    else constants.TYPE_TRANSITIVE
                )

    # If tracking import stacks, move them to the "stack" key in the output
    if track_import_stack:
        for mod, (deps, _) in flattened_deps.items():
            for dep_name, dep_stacks in deps.items():
                deps_out.setdefault(mod, {}).setdefault(dep_name, {})[
                    constants.INFO_STACK
                ] = dep_stacks

    # If showing optional, add the optional status of each dependency
    if show_optional:
        for mod, (deps, optional_mapping) in flattened_deps.items():
            for dep_name, dep_stacks in deps.items():
                deps_out.setdefault(mod, {}).setdefault(dep_name, {})[
                    constants.INFO_OPTIONAL
                ] = optional_mapping.get(dep_name, False)

    log.debug("Final output: %s", deps_out)
    return deps_out


## Private #####################################################################


def _get_dylib_dir():
    """Different versions/builds of python manage different builtin libraries as
    "builtins" versus extensions. As such, we need some heuristics to try to
    find the base directory that holds shared objects from the standard library.
    """
    is_dylib = lambda x: x is not None and (x.endswith(".so") or x.endswith(".dylib"))
    all_mod_paths = list(
        filter(is_dylib, (getattr(mod, "__file__", "") for mod in sys.modules.values()))
    )
    # If there's any dylib found, return the parent directory
    sample_dylib = None
    if all_mod_paths:
        sample_dylib = all_mod_paths[0]
    else:  # pragma: no cover
        # If not found with the above, look through libraries that are known to
        # sometimes be packaged as compiled extensions
        #
        # NOTE: This code may be unnecessary, but it is intended to catch future
        #   cases where the above does not yield results
        #
        # More names can be added here as needed
        for lib_name in ["cmath"]:
            lib = importlib.import_module(lib_name)
            fname = getattr(lib, "__file__", None)
            if is_dylib(fname):
                sample_dylib = fname
                break

    # If all else fails, we'll just return a sentinel string. This will fail to
    # match in the below check for builtin modules
    return (
        os.path.realpath(os.path.dirname(sample_dylib))
        if sample_dylib is not None
        else "BADPATH"
    )


# The path where global modules are found
_std_lib_dir = os.path.realpath(os.path.dirname(os.__file__))
_std_dylib_dir = _get_dylib_dir()
_known_std_pkgs = [
    "collections",
]


# Regex for matching lines in the exception table
_exception_table_expr = re.compile(r"  ([0-9]+) to ([0-9]+) -> [0-9]+ \[([0-9]+)\].*")


def _mod_defined_in_init_file(mod: ModuleType) -> bool:
    """Determine if the given module is defined in an __init__.py[c]"""
    mod_file = getattr(mod, "__file__", None)
    if mod_file is None:
        return False
    return os.path.splitext(os.path.basename(mod_file))[0] == "__init__"


def _get_import_parent_path(mod_name: str) -> str:
    """Get the parent directory of the given module"""
    mod = sys.modules[mod_name]  # NOTE: Intentionally unsafe to raise if not there!

    # Some standard libs have no __file__ attribute
    file_path = getattr(mod, "__file__", None)
    if file_path is None:
        return _std_lib_dir

    # If the module comes from an __init__, we need to pop two levels off
    if _mod_defined_in_init_file(mod):
        file_path = os.path.dirname(file_path)
    parent_path = os.path.dirname(file_path)
    return parent_path


def _is_third_party(mod_name: str) -> bool:
    """Detect whether the given module is a third party (non-standard and not
    import_tracker)"""
    mod_pkg = mod_name.partition(".")[0]
    return (
        not mod_name.startswith("_")
        and (
            mod_name not in sys.modules
            or _get_import_parent_path(mod_name) not in [_std_lib_dir, _std_dylib_dir]
        )
        and mod_pkg != constants.THIS_PACKAGE
        and mod_pkg not in _known_std_pkgs
    )


def _get_non_std_modules(mod_names: Iterable[str]) -> Set[str]:
    """Take a snapshot of the non-standard modules currently imported"""
    # Determine the names from the list that are non-standard
    return {mod_name for mod_name in mod_names if _is_third_party(mod_name)}


def _get_value_col(dis_line: str) -> str:
    """Parse the string value from a `dis` output line"""
    loc = dis_line.find("(")
    if loc >= 0:
        return dis_line[loc + 1 : -1]
    return ""


def _get_op_number(dis_line: str) -> Optional[int]:
    """Get the opcode number out of the line of `dis` output"""
    line_parts = dis_line.split()
    valid_line_part_idxs = [i for i, val in enumerate(line_parts) if val.isupper()]
    if not valid_line_part_idxs:
        return None
    opcode_idx = min(valid_line_part_idxs)
    assert opcode_idx > 0, f"Opcode found at the beginning of line! [{dis_line}]"
    return int(line_parts[opcode_idx - 1])


def _get_try_end_number(
    dis_line: str,
    op_num: Optional[int],
    exception_table: Dict[int, int],
) -> Optional[int]:
    """If the line contains a known indicator for a try block, get the
    corresponding end number

    NOTE: This contains compatibility code for changes between 3.10 and 3.11
    """
    return exception_table.get(op_num or -1) or (
        int(_get_value_col(dis_line).split()[-1])
        if any(op in dis_line for op in ["SETUP_FINALLY", "SETUP_EXCEPT"])
        else None
    )


def _get_exception_table(dis_lines: List[str]) -> Dict[int, int]:
    """For 3.11+ exception handling, parse the Exception Table"""
    table_start = [i for i, line in enumerate(dis_lines) if line == "ExceptionTable:"]
    assert len(table_start) <= 1, "Found multiple exception tables!"
    return (
        {
            int(m.group(1)): int(m.group(2))
            for m in [
                _exception_table_expr.match(line)
                for line in dis_lines[table_start[0] + 1 :]
            ]
            if m and int(m.group(3)) == 0 and m.group(1) != m.group(2)
        }
        if table_start
        else {}
    )


def _figure_out_import(
    mod: ModuleType,
    dots: Optional[int],
    import_name: Optional[str],
    import_from: Optional[str],
) -> ModuleType:
    """This function takes the set of information about an individual import
    statement parsed out of the `dis` output and attempts to find the in-memory
    module object it refers to.
    """
    log.debug2("Figuring out import [%s/%s/%s]", dots, import_name, import_from)

    # If there are no dots, look for candidate absolute imports
    if not dots:
        if import_name in sys.modules:
            if import_from is not None:
                candidate = f"{import_name}.{import_from}"
                if candidate in sys.modules:
                    log.debug3("Found [%s] in sys.modules", candidate)
                    return sys.modules[candidate]
            log.debug3("Found [%s] in sys.modules", import_name)
            return sys.modules[import_name]

    # Try simulating a relative import from a non-relative local
    dots = dots or 1

    # If there are dots, figure out the parent
    parent_mod_name_parts = mod.__name__.split(".")
    defined_in_init = _mod_defined_in_init_file(mod)
    if dots > 1:
        parent_dots = dots - 1 if defined_in_init else dots
        root_mod_name = ".".join(parent_mod_name_parts[:-parent_dots])
    elif defined_in_init:
        root_mod_name = mod.__name__
    else:
        root_mod_name = ".".join(parent_mod_name_parts[:-1])
    log.debug3("Parent mod name parts: %s", parent_mod_name_parts)
    log.debug3("Num Dots: %d", dots)
    log.debug3("Root mod name: %s", root_mod_name)
    log.debug3("Module file: %s", getattr(mod, "__file__", None))
    if not import_name:
        import_name = root_mod_name
    elif root_mod_name:
        import_name = f"{root_mod_name}.{import_name}"

    # Try with the import_from attached. This might be a module name or a
    # non-module attribute, so this might not work
    full_import_candidate = f"{import_name}.{import_from}"
    log.debug3("Looking for [%s] in sys.modules", full_import_candidate)
    if full_import_candidate in sys.modules:
        return sys.modules[full_import_candidate]

    # If that didn't work, the from is an attribute, so just get the import name
    return sys.modules.get(import_name)


def _get_imports(mod: ModuleType) -> Tuple[Set[ModuleType], Set[ModuleType]]:
    """Get the sets of required and optional imports for the given module by
    parsing its bytecode
    """
    log.debug2("Getting imports for %s", mod.__name__)
    req_imports = set()
    opt_imports = set()

    # Attempt to disassemble the byte code for this module. If the module has no
    # code, we ignore it since it's most likely a c extension
    try:
        loader = mod.__loader__ or mod.__spec__.loader
        mod_code = loader.get_code(mod.__name__)
    except (AttributeError, ImportError):
        log.warning("Couldn't find a loader for %s!", mod.__name__)
        return req_imports, opt_imports
    if mod_code is None:
        log.debug2("No code object found for %s", mod.__name__)
        return req_imports, opt_imports
    bcode = dis.Bytecode(mod_code)

    # Parse all bytecode lines
    current_dots = None
    current_import_name = None
    current_import_from = None
    open_import = False
    open_tries = set()
    log.debug4("Byte Code:")
    dis_lines = bcode.dis().split("\n")

    # Look for and parse an Exception Table (3.11+)
    exception_table = _get_exception_table(dis_lines)
    log.debug4("Exception Table: %s", exception_table)

    for line in dis_lines:
        log.debug4(line)
        line_val = _get_value_col(line)

        # If this is the beginning of a try block, add the end to the known open
        # try set
        op_num = _get_op_number(line)
        try_end = _get_try_end_number(line, op_num, exception_table)
        if try_end:
            open_tries.add(try_end)
            log.debug3("Open tries: %s", open_tries)

        # Parse the individual ops
        if "LOAD_CONST" in line:
            if line_val.isnumeric():
                current_dots = int(line_val)
        elif "IMPORT_NAME" in line:
            open_import = True
            current_import_name = line_val
        elif "IMPORT_FROM" in line:
            open_import = True
            current_import_from = line_val
        else:
            # This closes an import, so figure out what the module is that is
            # being imported!
            if open_import:
                import_mod = _figure_out_import(
                    mod, current_dots, current_import_name, current_import_from
                )
                if import_mod is not None:
                    log.debug2("Adding import module [%s]", import_mod.__name__)
                    if open_tries:
                        log.debug(
                            "Found optional dependency of [%s]: %s",
                            mod.__name__,
                            import_mod.__name__,
                        )
                        opt_imports.add(import_mod)
                    else:
                        req_imports.add(import_mod)

            # If this is a STORE_NAME, subsequent "from" statements may use the
            # same dots and name
            if "STORE_NAME" not in line:
                current_dots = None
                current_import_name = None
            open_import = False
            current_import_from = None

        # Close the open try if this ends one
        if op_num in open_tries:
            open_tries.remove(op_num)
            log.debug3("Closed try %d. Remaining open tries: %s", op_num, open_tries)

    # To the best of my knowledge, all bytecode will end with something other
    # than an import, even if an import is the last line in the file (e.g.
    # STORE_NAME). If this somehow proves to be untrue, please file a bug!
    assert not open_import, "Found an unclosed import in {}! {}/{}/{}".format(
        mod.__name__,
        current_dots,
        current_import_name,
        current_import_from,
    )

    return req_imports, opt_imports


def _find_parent_direct_deps(
    module_deps_map: Dict[str, List[str]]
) -> Dict[str, Dict[str, List[str]]]:
    """Construct a mapping for each module (e.g. foo.bar.baz) to a mapping of
    parent modules (e.g. [foo, foo.bar]) and the sets of imports that are
    directly imported in those modules. This mapping is used to augment the sets
    of required imports for each target module in the final flattening.
    """

    parent_direct_deps = {}
    for mod_name, mod_deps in module_deps_map.items():

        # Look through all parent modules of module_name and aggregate all
        # third-party deps that are directly used by those modules
        mod_base_name = mod_name.partition(".")[0]
        mod_name_parts = mod_name.split(".")
        for i in range(1, len(mod_name_parts)):
            parent_mod_name = ".".join(mod_name_parts[:i])
            parent_deps = module_deps_map.get(parent_mod_name, {})
            for dep, parent_dep_opt in parent_deps.items():
                currently_optional = mod_deps.get(dep, True)
                if not dep.startswith(mod_base_name) and currently_optional:
                    log.debug3(
                        "Adding direct-dependency of parent mod [%s] to [%s]: %s",
                        parent_mod_name,
                        mod_name,
                        dep,
                    )
                    mod_deps[dep] = currently_optional and parent_dep_opt
                    parent_direct_deps.setdefault(mod_name, {}).setdefault(
                        parent_mod_name, set()
                    ).add(dep)
    log.debug3("Parent direct dep map: %s", parent_direct_deps)
    return parent_direct_deps


def _flatten_deps(
    module_name: str,
    module_deps_map: Dict[str, List[str]],
    parent_direct_deps: Dict[str, Dict[str, List[str]]],
) -> Tuple[Dict[str, List[str]], Dict[str, bool]]:
    """Flatten the names of all modules that the target module depends on"""

    # Look through all modules that are directly required by this target module.
    # This only looks at the leaves, so if the module depends on foo.bar.baz,
    # only the deps for foo.bar.baz will be incluced and not foo.bar.buz or
    # foo.biz.
    all_deps = {}
    mods_to_check = {module_name: []}
    while mods_to_check:
        next_mods_to_check = {}
        for mod_to_check, parent_path in mods_to_check.items():
            log.debug4("Checking mod %s", mod_to_check)
            mod_parents_direct_deps = parent_direct_deps.get(mod_to_check, {})
            mod_path = parent_path + [mod_to_check]
            mod_deps = set(module_deps_map.get(mod_to_check, []))
            log.debug4(
                "Mod deps for %s at path %s: %s", mod_to_check, mod_path, mod_deps
            )
            new_mods = mod_deps - set(all_deps.keys())
            next_mods_to_check.update({new_mod: mod_path for new_mod in new_mods})
            for mod_dep in mod_deps:
                # If this is a parent direct dep, and the stack for this parent
                # is not already present in the dep stacks for this dependency,
                # add the parent to the path
                mod_dep_direct_parents = {}
                for (
                    mod_parent,
                    mod_parent_direct_deps,
                ) in mod_parents_direct_deps.items():
                    if mod_dep in mod_parent_direct_deps:
                        log.debug4(
                            "Found direct parent dep for [%s] from parent [%s] and dep [%s]",
                            mod_to_check,
                            mod_parent,
                            mod_dep,
                        )
                        mod_dep_direct_parents[mod_parent] = [
                            mod_parent
                        ] in all_deps.get(mod_dep, [])
                if mod_dep_direct_parents:
                    for (
                        mod_dep_direct_parent,
                        already_present,
                    ) in mod_dep_direct_parents.items():
                        if not already_present:
                            all_deps.setdefault(mod_dep, []).append(
                                [mod_dep_direct_parent] + mod_path
                            )
                else:
                    all_deps.setdefault(mod_dep, []).append(mod_path)
        log.debug3("Next mods to check: %s", next_mods_to_check)
        mods_to_check = next_mods_to_check
    log.debug4("All deps: %s", all_deps)

    # Create the flattened dependencies with the source lists for each
    mod_base_name = module_name.partition(".")[0]
    flat_base_deps = {}
    optional_deps_map = {}
    for dep, dep_sources in all_deps.items():
        if not dep.startswith(mod_base_name):
            # Truncate the dep_sources entries and trim to avoid duplicates
            dep_root_mod_name = dep.partition(".")[0]
            flat_dep_sources = flat_base_deps.setdefault(dep_root_mod_name, [])
            opt_dep_values = optional_deps_map.setdefault(dep_root_mod_name, [])
            for dep_source in dep_sources:
                log.debug4("Considering dep source list for %s: %s", dep, dep_source)

                # If any link in the dep_source is optional, the whole
                # dep_source should be considered optional
                is_optional = False
                for parent_idx, dep_mod in enumerate(dep_source[1:] + [dep]):
                    dep_parent = dep_source[parent_idx]
                    log.debug4(
                        "Checking whether [%s -> %s] is optional (dep=%s)",
                        dep_parent,
                        dep_mod,
                        dep_root_mod_name,
                    )
                    if module_deps_map.get(dep_parent, {}).get(dep_mod, False):
                        log.debug4("Found optional link %s -> %s", dep_parent, dep_mod)
                        is_optional = True
                        break
                opt_dep_values.append(
                    [
                        is_optional,
                        dep_source,
                    ]
                )

                flat_dep_source = dep_source
                if dep_root_mod_name in dep_source:
                    flat_dep_source = dep_source[: dep_source.index(dep_root_mod_name)]
                if flat_dep_source not in flat_dep_sources:
                    flat_dep_sources.append(flat_dep_source)
    log.debug3("Optional deps map for [%s]: %s", module_name, optional_deps_map)
    optional_deps_map = {
        mod: all([opt_val[0] for opt_val in opt_vals])
        for mod, opt_vals in optional_deps_map.items()
    }
    return flat_base_deps, optional_deps_map


## AST Import Tracking #########################################################


if isinstance(__builtins__, dict):  # pragma: no cover - depends on runner
    _BUILTIN_NAMES = set(__builtins__.keys())
else:
    _BUILTIN_NAMES = set(dir(__builtins__))
_BUILTIN_NAMES.update(
    {
        "__file__",
        "__name__",
        "__doc__",
        "__package__",
        "__loader__",
        "__spec__",
        "__builtins__",
        "__cached__",
    }
)


def _read_source(
    source: Union[str, "os.PathLike[str]"], is_file: Optional[bool]
) -> Tuple[Optional[str], str]:
    """Return (filename, source_text) for the given source argument"""
    path_str = os.fspath(source)
    if is_file is True or (is_file is None and os.path.isfile(path_str)):
        with tokenize.open(path_str) as handle:
            return os.path.abspath(path_str), handle.read()
    return None, source


@dataclass
class _RawImport:
    """An unmerged import binding as found during the single AST walk"""

    name: str
    module: Optional[str]
    imported: Optional[str]
    lineno: int
    col: int
    scope: "_Scope"
    branches: Tuple[Tuple[str, bool], ...]
    star: bool = False
    used: bool = False


@dataclass
class _Load:
    """A name load as found during the single AST walk"""

    name: str
    lineno: int
    col: int
    scope: "_Scope"
    annotation: bool


@dataclass
class _Scope:
    """One lexical scope collected during the single AST walk"""

    scope_id: int
    kind: str
    label: str
    qualified: str
    depth: int
    parent: Optional["_Scope"]
    bindings: Set[str] = field(default_factory=set)
    globals: Set[str] = field(default_factory=set)
    nonlocals: Set[str] = field(default_factory=set)
    imports: List[_RawImport] = field(default_factory=list)
    stars: List[_RawImport] = field(default_factory=list)
    loads: List[_Load] = field(default_factory=list)


def _safe_unparse(node: Optional[ast.AST]) -> str:
    """Best-effort compact source rendering of an expression node"""
    if node is None:
        return ""
    try:
        return re.sub(r"\s+", " ", ast.unparse(node)).strip()
    except Exception:  # pragma: no cover - unparse is robust on 3.9+
        return type(node).__name__


def _is_type_checking_test(node: ast.AST) -> bool:
    """True if the expression is a plain ``TYPE_CHECKING`` style reference"""
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    if isinstance(node, ast.Attribute):
        return node.attr == "TYPE_CHECKING"
    return False


class _ImportTrackingVisitor(ast.NodeVisitor):
    """Single-pass AST visitor recording scopes, imports and name loads"""

    def __init__(self) -> None:
        self.module_scope = _Scope(
            scope_id=0,
            kind="module",
            label="<module>",
            qualified="<module>",
            depth=0,
            parent=None,
        )
        self.scopes: List[_Scope] = [self.module_scope]
        self.scope_stack: List[_Scope] = [self.module_scope]
        self.branch_stack: List[Tuple[str, bool]] = []
        self.next_scope_id = 1
        self._annotation_depth = 0

    @property
    def scope(self) -> _Scope:
        return self.scope_stack[-1]

    def _push_scope(self, kind: str, label: str) -> _Scope:
        parent = self.scope_stack[-1]
        scope = _Scope(
            scope_id=self.next_scope_id,
            kind=kind,
            label=label,
            qualified=f"{parent.qualified}::{label}",
            depth=len(self.scope_stack),
            parent=parent,
        )
        self.next_scope_id += 1
        self.scopes.append(scope)
        self.scope_stack.append(scope)
        return scope

    def _pop_scope(self) -> None:
        self.scope_stack.pop()

    def _branch_block(
        self, label: str, type_checking: bool, statements: Iterable[ast.AST]
    ) -> None:
        self.branch_stack.append((label, type_checking))
        try:
            for statement in statements:
                self.visit(statement)
        finally:
            self.branch_stack.pop()

    def _record_import(
        self,
        node: Union[ast.Import, ast.ImportFrom],
        module: Optional[str],
        imported: Optional[str],
        name: str,
        star: bool = False,
    ) -> None:
        raw = _RawImport(
            name=name,
            module=module,
            imported=imported,
            lineno=node.lineno,
            col=node.col_offset,
            scope=self.scope,
            branches=tuple(self.branch_stack),
            star=star,
        )
        if star:
            self.scope.stars.append(raw)
        else:
            self.scope.imports.append(raw)
            self.scope.bindings.add(name)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self._record_import(node, alias.name, None, bound)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = ("." * (node.level or 0)) + (node.module or "")
        for alias in node.names:
            if alias.name == "*":
                self._record_import(node, module, "*", "*", star=True)
            else:
                bound = alias.asname or alias.name
                self._record_import(node, module, alias.name, bound)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.scope.bindings.add(node.id)
        else:
            self.scope.loads.append(
                _Load(
                    name=node.id,
                    lineno=node.lineno,
                    col=node.col_offset,
                    scope=self.scope,
                    annotation=self._annotation_depth > 0,
                )
            )
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.scope.nonlocals.update(node.names)

    def _bind_targets(self, target: ast.AST) -> None:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                self.scope.bindings.add(sub.id)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        self._collect_annotation(node.annotation)
        if node.value is not None:
            self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)

    def _visit_loop(self, node: ast.AST) -> None:
        self.visit(node.iter)
        self.visit(node.target)
        label = f"for:{_safe_unparse(node.iter)}"
        self._branch_block(label, False, node.body)
        if node.orelse:
            self._branch_block("for:else", False, node.orelse)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._branch_block(f"while:{_safe_unparse(node.test)}", False, node.body)
        if node.orelse:
            self._branch_block("while:else", False, node.orelse)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
        label = "with:" + ",".join(
            _safe_unparse(item.context_expr) for item in node.items
        )
        self._branch_block(label, False, node.body)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self.scope.bindings.add(node.name)
        label = (
            f"except:{_safe_unparse(node.type)}"
            if node.type is not None
            else "except"
        )
        self._branch_block(label, False, node.body)

    @staticmethod
    def _all_arg_nodes(args: ast.arguments) -> List[ast.arg]:
        all_args: List[ast.arg] = []
        all_args.extend(args.posonlyargs)
        all_args.extend(args.args)
        all_args.extend(args.kwonlyargs)
        if args.vararg is not None:
            all_args.append(args.vararg)
        if args.kwarg is not None:
            all_args.append(args.kwarg)
        return all_args

    def _collect_annotation(self, annotation: ast.AST) -> None:
        """Visit an annotation expression in the current (enclosing) scope

        String forward references are parsed and treated as used.
        """
        self._annotation_depth += 1
        try:
            if isinstance(annotation, ast.Constant) and isinstance(
                annotation.value, str
            ):
                self._collect_string_annotation(annotation.value)
            else:
                self.visit(annotation)
        finally:
            self._annotation_depth -= 1

    def _collect_string_annotation(self, value: str) -> None:
        try:
            parsed = ast.parse(value, mode="eval")
        except SyntaxError:
            return
        self.visit(parsed.body)

    def _visit_arguments_enclosing(self, args: ast.arguments) -> None:
        """Visit defaults in the enclosing scope, annotations included"""
        for default in args.defaults:
            self.visit(default)
        for default in args.kw_defaults:
            if default is not None:
                self.visit(default)
        for arg in self._all_arg_nodes(args):
            if arg.annotation is not None:
                self._collect_annotation(arg.annotation)

    @staticmethod
    def _param_names(args: ast.arguments) -> List[str]:
        names = [arg.arg for arg in _ImportTrackingVisitor._all_arg_nodes(args)]
        return names

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(
        self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_arguments_enclosing(node.args)
        if node.returns is not None:
            self._collect_annotation(node.returns)
        self.scope.bindings.add(node.name)
        scope = self._push_scope("function", f"{type(node).__name__}:{node.name}")
        try:
            scope.bindings.update(self._param_names(node.args))
            for statement in node.body:
                self.visit(statement)
        finally:
            self._pop_scope()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)
        for arg in self._all_arg_nodes(node.args):
            if arg.annotation is not None:
                self._collect_annotation(arg.annotation)
        scope = self._push_scope("function", "Lambda")
        try:
            scope.bindings.update(self._param_names(node.args))
            self.visit(node.body)
        finally:
            self._pop_scope()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self.scope.bindings.add(node.name)
        scope = self._push_scope("class", f"ClassDef:{node.name}")
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._pop_scope()

    def _visit_comprehension(
        self,
        node: ast.AST,
        generators: List[ast.comprehension],
        *results: ast.AST,
    ) -> None:
        first = generators[0]
        self.visit(first.iter)
        scope = self._push_scope("function", type(node).__name__)
        try:
            for idx, comp in enumerate(generators):
                if idx > 0:
                    self.visit(comp.iter)
                self._bind_targets(comp.target)
                for condition in comp.ifs:
                    self.visit(condition)
            for result in results:
                self.visit(result)
        finally:
            self._pop_scope()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node, node.generators, node.elt)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node, node.generators, node.elt)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node, node.generators, node.elt)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node, node.generators, node.key, node.value)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        body_tc = _is_type_checking_test(node.test)
        body_label = (
            "if TYPE_CHECKING"
            if body_tc
            else f"if:{_safe_unparse(node.test)}"
        )
        self._branch_block(body_label, body_tc, node.body)
        if node.orelse:
            if (
                len(node.orelse) == 1
                and isinstance(node.orelse[0], ast.If)
            ):
                self.visit(node.orelse[0])
            else:
                else_tc = isinstance(node.test, ast.UnaryOp) and isinstance(
                    node.test.op, ast.Not
                ) and _is_type_checking_test(node.test.operand)
                self._branch_block(
                    "else",
                    else_tc,
                    node.orelse,
                )

    def visit_Try(self, node: ast.Try) -> None:
        self._branch_block("try", False, node.body)
        for handler in node.handlers:
            self.visit(handler)
        self._branch_block("try:else", False, node.orelse)
        self._branch_block("finally", False, node.finalbody)

    if hasattr(ast, "TryStar"):

        def visit_TryStar(self, node: ast.TryStar) -> None:
            self.visit_Try(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self._visit_pattern_values(case.pattern)
            self._match_pattern_bindings(case.pattern)
            label = f"case:{_safe_unparse(case.pattern)}"
            if case.guard is not None:
                self.visit(case.guard)
            self._branch_block(label, False, case.body)

    def _visit_pattern_values(self, pattern: ast.AST) -> None:
        """Visit only the runtime-value parts of a match pattern"""
        if isinstance(pattern, ast.MatchValue):
            self.visit(pattern.value)
        elif isinstance(pattern, ast.MatchSingleton):
            return
        elif isinstance(pattern, ast.MatchSequence):
            for sub in pattern.patterns:
                self._visit_pattern_values(sub)
        elif isinstance(pattern, ast.MatchMapping):
            for key in pattern.keys:
                self.visit(key)
            for sub in pattern.patterns:
                self._visit_pattern_values(sub)
        elif isinstance(pattern, ast.MatchClass):
            self.visit(pattern.cls)
            for sub in pattern.patterns:
                self._visit_pattern_values(sub)
            for kwd in pattern.kwd_patterns:
                self._visit_pattern_values(kwd)
        elif isinstance(pattern, ast.MatchOr):
            for sub in pattern.patterns:
                self._visit_pattern_values(sub)

    def _match_pattern_bindings(self, pattern: Optional[ast.AST]) -> None:
        if pattern is None:
            return
        if isinstance(pattern, ast.MatchAs) and pattern.name is not None:
            self.scope.bindings.add(pattern.name)
        for child in ast.iter_child_nodes(pattern):
            self._match_pattern_bindings(child)

    if hasattr(ast, "TypeAlias"):

        def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
            self.visit(node.name)
            self._collect_annotation(node.value)

    def _resolution_chain(self, scope: _Scope) -> List[_Scope]:
        """Lexical resolution chain, skipping class scopes from functions"""
        chain: List[_Scope] = []
        current: Optional[_Scope] = scope
        while current is not None:
            chain.append(current)
            next_scope = current.parent
            parent = current.parent
            if current.kind in ("function",) and parent is not None:
                while parent is not None and parent.kind == "class":
                    parent = parent.parent
                next_scope = parent
            current = next_scope
        return chain

    def build_report(self) -> ImportReport:
        """Resolve names against collected scopes and build the final report"""
        missing: List[Tuple[str, int, int, _Scope, Optional[str]]] = []
        for scope in self.scopes:
            for load in scope.loads:
                self._resolve_load(load, scope, missing)

        entries: List[ImportEntry] = []
        groups: Dict[Tuple[int, str], List[_RawImport]] = {}
        for scope in self.scopes:
            for raw in scope.imports:
                groups.setdefault((scope.scope_id, raw.name), []).append(raw)
            for raw in scope.stars:
                groups.setdefault((scope.scope_id, f"*:{raw.lineno}:{raw.col}"), [raw])

        for _, raws in groups.items():
            entries.append(self._merge_raw_imports(raws))

        entries.sort(key=lambda entry: (entry.lineno, entry.col, entry.name))

        missing_records = []
        seen = set()
        for name, lineno, col, scope, attributed in missing:
            key = (name, lineno, col, scope.scope_id, attributed or "")
            if key in seen:
                continue
            seen.add(key)
            missing_records.append(
                MissingRef(
                    name=name,
                    lineno=lineno,
                    col=col,
                    scope_depth=scope.depth,
                    scope=scope.qualified,
                    attributed_to=attributed,
                )
            )
        missing_records.sort(key=lambda ref: (ref.lineno, ref.col, ref.name))

        return ImportReport(
            imports=tuple(entries),
            missing=tuple(missing_records),
        )

    def _resolve_load(
        self,
        load: _Load,
        scope: _Scope,
        missing: List[Tuple[str, int, int, _Scope, Optional[str]]],
    ) -> None:
        name = load.name
        if name in _BUILTIN_NAMES:
            return
        chain = self._resolution_chain(scope)
        if load.annotation:
            for candidate in chain:
                hit = next(
                    (raw for raw in candidate.imports if raw.name == name), None
                )
                if hit is not None:
                    hit.used = True
                    return
                if name in candidate.bindings:
                    return
                if candidate.stars:
                    candidate.stars[0].used = True
                    return
            return
        if name in scope.globals:
            hit = next(
                (raw for raw in self.module_scope.imports if raw.name == name),
                None,
            )
            if hit is not None:
                hit.used = True
                return
            if name in self.module_scope.bindings:
                return
            missing.append((name, load.lineno, load.col, scope, None))
            return
        if name in scope.nonlocals:
            for outer in chain[1:]:
                if outer.kind != "function":
                    continue
                hit = next((raw for raw in outer.imports if raw.name == name), None)
                if hit is not None:
                    hit.used = True
                    return
                if name in outer.bindings:
                    return
            missing.append((name, load.lineno, load.col, scope, None))
            return
        for candidate in chain:
            hit = next((raw for raw in candidate.imports if raw.name == name), None)
            if hit is not None:
                hit.used = True
                return
            if name in candidate.bindings:
                return
            stars = candidate.stars
            if stars:
                for star in stars:
                    star.used = True
                missing.append(
                    (name, load.lineno, load.col, scope, stars[0].module)
                )
                return
        missing.append((name, load.lineno, load.col, scope, None))

    @staticmethod
    def _merge_raw_imports(raws: List[_RawImport]) -> ImportEntry:
        winner = min(
            raws,
            key=lambda raw: (
                len(raw.branches),
                any(tc for _, tc in raw.branches),
                raw.lineno,
                raw.col,
            ),
        )
        scope = winner.scope
        if not winner.branches:
            branches: Tuple[Tuple[str, ...], ...] = tuple()
        else:
            paths = sorted(
                {
                    tuple(label for label, _ in raw.branches)
                    for raw in raws
                    if raw.branches
                }
            )
            branches = tuple(paths)
        type_checking = bool(
            winner.branches
        ) and all(tc for _, tc in winner.branches)
        return ImportEntry(
            name=winner.name,
            module=winner.module or "",
            imported=winner.imported,
            lineno=winner.lineno,
            col=winner.col,
            scope_depth=scope.depth,
            scope=scope.qualified,
            branches=branches,
            type_checking=type_checking,
            used=any(raw.used for raw in raws),
        )


def _render_import_statement(entry: ImportEntry) -> str:
    """Render an entry back into a canonical import statement"""
    if entry.imported is None:
        root = entry.module.split(".")[0]
        if entry.name == root:
            return f"import {entry.module}"
        return f"import {entry.module} as {entry.name}"
    if entry.imported == "*":
        return f"from {entry.module} import *"
    if entry.name == entry.imported:
        return f"from {entry.module} import {entry.imported}"
    return f"from {entry.module} import {entry.imported} as {entry.name}"


def _render_report(report: ImportReport, indent: int = 2) -> str:
    """Render an indented, deterministic attribution listing for one file"""
    lines: List[str] = []
    current_scope: Optional[str] = None
    for entry in report.imports:
        if entry.scope != current_scope:
            current_scope = entry.scope
            lines.append(
                " " * (indent * entry.scope_depth)
                + f"scope: {entry.scope} (depth {entry.scope_depth})"
            )
        branch_text = ""
        if entry.branches and any(entry.branches):
            paths = [" > ".join(path) for path in entry.branches if path]
            branch_text = f" [branch: {' | '.join(paths)}]"
        tc_text = " [TYPE_CHECKING]" if entry.type_checking else ""
        status = "used" if entry.used else "UNUSED"
        lines.append(
            " " * (indent * (entry.scope_depth + 1))
            + f"{_render_import_statement(entry)}  # {status}"
            + tc_text
            + branch_text
        )
    if report.missing:
        lines.append("missing references:")
        for ref in report.missing:
            attributed = (
                f" (attributed to `from {ref.attributed_to} import *`)"
                if ref.attributed_to
                else ""
            )
            lines.append(
                f"  line {ref.lineno}:{ref.col} `{ref.name}` "
                f"in {ref.scope}{attributed}"
            )
    if report.unused:
        lines.append("unused imports:")
        for entry in report.unused:
            lines.append(
                f"  line {entry.lineno} {_render_import_statement(entry)} "
                f"in {entry.scope}"
            )
    return "\n".join(lines)
