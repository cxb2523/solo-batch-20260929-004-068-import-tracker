"""
This module implements utilities that enable tracking of third party deps
through import statements
"""
# Standard
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union
import ast
import builtins
import dis
import importlib
import os
import re
import sys

# Local
from . import constants
from .log import log

## Public ######################################################################


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


## AST-Based Single-File Import Tracking #######################################


def track_imports(source: str, filename: str = "<unknown>") -> Dict[str, Any]:
    """Track the imports of a single python source file with a single AST
    traversal, attributing each import to its lexical scope and conditional
    branch path and determining which imports are actually used.

    Args:
        source:  str
            The python source code to analyze
        filename:  str
            The name of the file the source came from (used in the report only)

    Returns:
        report:  Dict[str, Any]
            A deterministic, JSON-serializable report with the keys:
                - filename: The filename passed in
                - imports: Ordered list of import entries. Each entry holds
                    "symbol", "module", "name", "alias", "scope", "depth",
                    "branches", "lineno", "star", "type_checking" and "used".
                - unused: Ordered list of unused import entries
                - unresolved: Sorted list of referenced names that could not be
                    attributed to any import or local binding
                - star_attributions: Mapping from names that could only be
                    resolved through a "from x import *" to the module x
    """
    tree = ast.parse(source, filename=filename)
    visitor = _ImportVisitor()
    visitor.visit(tree)
    unresolved, star_attributions = _resolve_usage(visitor.all_scopes)

    imports = []
    unused = []
    for scope in visitor.all_scopes:
        for entry in scope.symbols.values():
            imports.append(
                {key: val for key, val in entry.items() if not key.startswith("_")}
            )
            if not entry["used"] and not entry["star"]:
                unused.append(
                    {
                        "symbol": entry["symbol"],
                        "module": entry["module"],
                        "scope": entry["scope"],
                        "lineno": entry["lineno"],
                    }
                )

    return {
        "filename": filename,
        "imports": imports,
        "unused": unused,
        "unresolved": sorted(unresolved),
        "star_attributions": {
            name: star_attributions[name] for name in sorted(star_attributions)
        },
    }


def format_import_report(report: Dict[str, Any], indent: int = 2) -> str:
    """Format the report produced by track_imports as an indented attribution
    listing grouped by scope for manual inspection.
    """
    unit = " " * indent
    lines = []
    current_scope = None
    for entry in report["imports"]:
        if entry["scope"] != current_scope:
            current_scope = entry["scope"]
            lines.append("{}{}:".format(unit * entry["depth"], current_scope or "<module>"))
        status = "used" if entry["used"] else "unused"
        line = "{}{} -> {} [{}]".format(
            unit * (entry["depth"] + 1), entry["symbol"], entry["module"], status
        )
        if entry["branches"]:
            line += " (branches: {})".format(", ".join(entry["branches"]))
        lines.append(line)
    if report["star_attributions"]:
        lines.append("star attributions:")
        for name, module in report["star_attributions"].items():
            lines.append("{}{} -> {} (via star import)".format(unit, name, module))
    if report["unresolved"]:
        lines.append("unresolved: " + ", ".join(report["unresolved"]))
    return "\n".join(lines)


class _Scope:
    """A single lexical scope discovered while walking the AST"""

    __slots__ = (
        "kind",
        "path",
        "parent",
        "symbols",
        "bound",
        "used",
        "annotations",
        "tc_children",
    )

    def __init__(self, kind: str, path: Tuple[str, ...], parent: "_Scope" = None):
        self.kind = kind
        self.path = path
        self.parent = parent
        # Imported symbols bound in this scope: symbol -> entry (ordered)
        self.symbols = {}
        # Non-import names bound in this scope (assignments, defs, args, ...)
        self.bound = set()
        # Root names loaded at runtime directly in this scope
        self.used = set()
        # Names referenced from annotations (forward refs always count as used)
        self.annotations = set()
        # TYPE_CHECKING scopes directly nested in this scope
        self.tc_children = []


_BUILTIN_NAMES = set(dir(builtins))

_IDENTIFIER_EXPR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _is_str_const(node: ast.AST) -> bool:
    """Compatibility check for string constant nodes across python versions"""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    return node.__class__.__name__ == "Str"  # pragma: no cover (py<3.8)


def _names_from_string_annotation(value: str) -> Set[str]:
    """Extract the referenced names from a string (forward reference)
    annotation
    """
    try:
        expr = ast.parse(value, mode="eval")
    except SyntaxError:
        return set(_IDENTIFIER_EXPR.findall(value))
    names = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif _is_str_const(node):
            str_val = node.value if isinstance(node, ast.Constant) else node.s
            names.update(_names_from_string_annotation(str_val))
    return names


def _is_type_checking_test(test: ast.AST) -> bool:
    """Detect `if TYPE_CHECKING:` and `if typing.TYPE_CHECKING:`"""
    return (
        isinstance(test, ast.Name)
        and test.id == "TYPE_CHECKING"
        or isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
    )


class _ImportVisitor(ast.NodeVisitor):
    """Single-pass AST visitor that records imports, bindings and name usage
    per lexical scope along with the conditional branch path of each import
    """

    def __init__(self):
        self._scope_stack = [_Scope("module", ())]
        self._branch_stack = []
        self.all_scopes = [self._scope_stack[0]]

    @property
    def scope(self) -> _Scope:
        return self._scope_stack[-1]

    ## Scope management ##

    def _push_scope(self, kind: str, name: str):
        parent = self.scope
        scope = _Scope(kind, parent.path + (name,), parent)
        if kind == "type_checking":
            parent.tc_children.append(scope)
        self._scope_stack.append(scope)
        self.all_scopes.append(scope)

    def _pop_scope(self):
        self._scope_stack.pop()

    ## Import recording ##

    def _add_import(
        self,
        symbol: str,
        module: str,
        name: Optional[str],
        alias: Optional[str],
        lineno: int,
        star: bool = False,
    ):
        scope = self.scope
        branch = list(self._branch_stack)
        existing = scope.symbols.get(symbol)
        if existing is not None:
            # Merge repeated imports of the same symbol within this scope into
            # a single entry, accumulating the branch paths of all occurrences
            for label in branch:
                if label not in existing["branches"]:
                    existing["branches"].append(label)
            # On conflict, the top-level (unbranched) occurrence wins
            if existing["_branched"] and not branch:
                existing.update(
                    {
                        "module": module,
                        "name": name,
                        "alias": alias,
                        "lineno": lineno,
                        "star": star,
                        "_branched": False,
                    }
                )
            return
        scope.symbols[symbol] = {
            "symbol": symbol,
            "module": module,
            "name": name,
            "alias": alias,
            "scope": ".".join(scope.path),
            "depth": len(scope.path),
            "type_checking": scope.kind == "type_checking",
            "lineno": lineno,
            "branches": branch,
            "star": star,
            "used": False,
            "_branched": bool(branch),
        }

    ## Node visitors ##

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self._add_import(
                symbol=alias.asname or alias.name.partition(".")[0],
                module=alias.name,
                name=None,
                alias=alias.asname,
                lineno=node.lineno,
            )

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            if alias.name == "*":
                self._add_import(
                    symbol="*",
                    module=module,
                    name="*",
                    alias=None,
                    lineno=node.lineno,
                    star=True,
                )
            else:
                self._add_import(
                    symbol=alias.asname or alias.name,
                    module=module,
                    name=alias.name,
                    alias=alias.asname,
                    lineno=node.lineno,
                )

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load):
            self.scope.used.add(node.id)
        else:
            self.scope.bound.add(node.id)

    def visit_If(self, node: ast.If):
        self.visit(node.test)
        label = "if@{}".format(node.lineno)
        if _is_type_checking_test(node.test):
            self._push_scope("type_checking", "TYPE_CHECKING")
            self._branch_stack.append(label + ":body")
            for stmt in node.body:
                self.visit(stmt)
            self._branch_stack.pop()
            self._pop_scope()
        else:
            self._branch_stack.append(label + ":body")
            for stmt in node.body:
                self.visit(stmt)
            self._branch_stack.pop()
        self._branch_stack.append(label + ":orelse")
        for stmt in node.orelse:
            self.visit(stmt)
        self._branch_stack.pop()

    def _visit_try(self, node):
        label = "try@{}".format(node.lineno)
        self._branch_stack.append(label + ":body")
        for stmt in node.body:
            self.visit(stmt)
        self._branch_stack.pop()
        for idx, handler in enumerate(node.handlers):
            if handler.type:
                self.visit(handler.type)
            if handler.name:
                self.scope.bound.add(handler.name)
            self._branch_stack.append("{}:handler{}".format(label, idx))
            for stmt in handler.body:
                self.visit(stmt)
            self._branch_stack.pop()
        for part, stmts in (("orelse", node.orelse), ("finalbody", node.finalbody)):
            self._branch_stack.append("{}:{}".format(label, part))
            for stmt in stmts:
                self.visit(stmt)
            self._branch_stack.pop()

    def visit_Try(self, node: ast.Try):
        self._visit_try(node)

    def visit_TryStar(self, node):  # python 3.11+
        self._visit_try(node)

    def _record_annotation(self, annotation: Optional[ast.AST]):
        """Names referenced in annotations (including string forward
        references) always count as used
        """
        if annotation is None:
            return
        if _is_str_const(annotation):
            str_val = (
                annotation.value
                if isinstance(annotation, ast.Constant)
                else annotation.s
            )
            self.scope.annotations.update(_names_from_string_annotation(str_val))
            return
        self.visit(annotation)
        for sub_node in ast.walk(annotation):
            if _is_str_const(sub_node):
                str_val = (
                    sub_node.value
                    if isinstance(sub_node, ast.Constant)
                    else sub_node.s
                )
                self.scope.annotations.update(_names_from_string_annotation(str_val))

    def _visit_arguments(self, args: ast.arguments):
        all_args = list(getattr(args, "posonlyargs", []))
        all_args += list(args.args) + list(args.kwonlyargs)
        for arg in all_args + [args.vararg, args.kwarg]:
            if arg is None:
                continue
            self.scope.bound.add(arg.arg)
            self._record_annotation(arg.annotation)
        for default in list(args.defaults) + list(args.kw_defaults):
            if default is not None:
                self.visit(default)

    def _visit_function_def(self, node):
        self.scope.bound.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        # Signature annotations and defaults are evaluated in the enclosing
        # scope
        self._visit_arguments(node.args)
        self._record_annotation(node.returns)
        self._push_scope("function", node.name)
        for stmt in node.body:
            self.visit(stmt)
        self._pop_scope()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._visit_function_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._visit_function_def(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        self.scope.bound.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword)
        self._push_scope("class", node.name)
        for stmt in node.body:
            self.visit(stmt)
        self._pop_scope()

    def visit_Lambda(self, node: ast.Lambda):
        self._visit_arguments(node.args)
        self.visit(node.body)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        self._record_annotation(node.annotation)
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)


def _resolve_usage(scopes: List[_Scope]) -> Tuple[Set[str], Dict[str, str]]:
    """Walk every scope's referenced names up the scope chain, marking the
    imports they resolve to as used. Names that cannot be attributed to any
    import or binding are either conservatively attributed to a visible
    "from x import *" or reported as unresolved.
    """
    unresolved = set()
    star_attributions = {}
    for scope in scopes:
        for name in sorted(scope.used):
            _resolve_name(scope, name, False, unresolved, star_attributions)
        for name in sorted(scope.annotations):
            _resolve_name(scope, name, True, unresolved, star_attributions)
    return unresolved, star_attributions


def _resolve_name(
    scope: _Scope,
    name: str,
    is_annotation: bool,
    unresolved: Set[str],
    star_attributions: Dict[str, str],
):
    current = scope
    while current is not None:
        entry = current.symbols.get(name)
        if entry is not None:
            entry["used"] = True
            return
        if name in current.bound:
            return
        # Annotation (forward) references may resolve to imports that live in
        # an `if TYPE_CHECKING:` block nested in this scope
        if is_annotation:
            for tc_scope in current.tc_children:
                tc_entry = tc_scope.symbols.get(name)
                if tc_entry is not None:
                    tc_entry["used"] = True
                    return
        current = current.parent
    # Builtins and dunders are never missing references
    if name in _BUILTIN_NAMES or (name.startswith("__") and name.endswith("__")):
        return
    # Conservatively attribute unknown names to a visible "from x import *",
    # but only within the scopes where that star import is visible
    current = scope
    while current is not None:
        star_entry = current.symbols.get("*")
        if star_entry is not None:
            star_entry["used"] = True
            star_attributions.setdefault(name, star_entry["module"])
            return
        current = current.parent
    unresolved.add(name)
