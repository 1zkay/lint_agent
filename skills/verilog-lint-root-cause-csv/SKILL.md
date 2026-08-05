---
name: verilog-lint-root-cause-csv
description: Use for source-based Verilog/SystemVerilog lint root-cause analysis and global root consolidation in the prepared CSV workflow.
---

# Verilog Lint Root-Cause CSV

Perform only the role and write only the exact output path supplied by the
outer workflow. Treat generated paths as workflow artifacts, not as design
evidence. Do not prepare inputs, classify modules, build slices, sort, validate,
publish, or create timestamped reports.

Write every CSV with a CSV-aware writer instead of concatenating fields.
Preserve commas, quotes, and newlines with standard CSV quoting.

## Group roots and causal relationships

Group findings under one root only when they share the same underlying defect
mechanism and concrete repair strategy. Treat a shared lint rule, module, file,
level, hierarchy, or similar wording as neither sufficient evidence nor an
automatic barrier to grouping. Keep findings separate when either the mechanism
or repair differs.

Use a parent only when source, hierarchy, or signal-flow evidence establishes
that one distinct normal root directly causes or enables another root with a
different mechanism or repair.

- Merge a downstream finding into the upstream root when fixing the upstream
  root fully removes it and no separate repair remains.
- Otherwise, point the child to its nearest existing direct parent.
- Use `/` when direction is unproven or multiple parents remain plausible.
- Never invent a parent or attach `误报` to one.
- State the child's mechanism and direct causal link in `root_note`, and its
  distinct repair and required order in `fix_suggestion`.

## Analyze assigned work units

Use only the work-unit directories assigned in the current batch. Treat every
unit independently: never share a local root or root numbering across units,
and write each exact report target supplied by the workflow. A batch may contain
both module and instance units from the same slice level.

Within each unit:

- `lint.csv` exclusively owns every alert in the unit;
- `rtl/` contains physically copied source evidence;
- `filelist.f` preserves relevant source order, macros, and include paths;
- `context.json` identifies primary and dependency files;
- instance units also provide `hierarchy_tree.txt`.

Keep any temporary helper artifacts inside that unit's `work/` directory. Do
not read an unassigned unit or write outside the assigned units.

For every lint row, open `rtl/<source_file>` at `source_line` and inspect the
enclosing construct and relevant signal, parameter, instance, or control-flow
context. Never decide from `message_id` or `contents` alone. A nonempty
`hierarchy` is instance evidence; an empty value is module-scope evidence.
Semicolon-separated paths still represent one source warning.

When `lint.csv` is large, run
`python skills/verilog-lint-root-cause-csv/scripts/inspect_work_unit.py <work-unit-dir>`
to summarize it, page rows with `--offset` and `--limit`, or retrieve one row
with `--vio-id`. The helper only reads artifacts and makes no semantic decision.

Treat a finding as `误报` when the reported code is intentional, conforms to
the project design intent, requires no RTL change, and this conclusion is
supported by the reopened source and available context. If an RTL change is
required, use a normal root. If intent is unconfirmed, continue investigating.

Write the exact local report path with these columns:

```text
root_id,root_note,fix_suggestion,root_file_path,root_file_start,root_file_end,parent_root_id,leaf_violation_id,leaf_violation_note
```

Apply these rules:

- write exactly one row for each `lint.csv` row;
- set `leaf_violation_id=vio_id`;
- set `leaf_violation_note=message_id:contents` without changing either input;
- number normal local roots consecutively from `root_001`;
- write concise Chinese `root_note` and `fix_suggestion`;
- use a normalized POSIX path relative to the unit `rtl/` directory and valid
  1-based inclusive source lines for each leaf's concrete occurrence;
- repeat identical category fields for rows sharing a normal root;
- use `/` for an independent root's `parent_root_id`;
- for `误报`, explain the source-backed reason in `root_note` and use `/` for
  both `fix_suggestion` and `parent_root_id`.

After writing, perform a second pass over every row. Reopen its lint entry,
reported source location, and recorded root location. Confirm exact leaf
fields, valid locations, consistent grouping, supported false-positive
decisions, and valid parent relationships. Revise the same file until this
source-based review is complete.

## Consolidate local roots globally

Read every row of the supplied local-root catalog and write the exact global
mapping target with these columns:

```text
local_item_id,global_root_id,root_note,fix_suggestion,parent_global_root_id
```

Map every `local_item_id` exactly once. Maintain one defect-class view across
all work units and levels by applying the shared grouping and causal rules
above. Do not inspect another global mapping output.

- use work-unit reports and source evidence to resolve unclear catalog items;
- number normal global roots consecutively from `root_001`;
- keep each normal root's Chinese note, Chinese repair, and parent consistent;
- map a confirmed false positive to `误报`, retain a source-backed Chinese
  reason, and use `/` for repair and parent.

Do not merge by majority, textual similarity alone, or a shared level. Do not
change or omit a local item.

## Review global mapping proposals

Compare every supplied proposal row by row against the local-root catalog.
Resolve every `local_item_id`, root definition, grouping, false-positive
decision, and parent relationship. Agreement is not evidence. When proposals
differ or evidence is ambiguous, reopen the corresponding local report,
`lint.csv`, and RTL before deciding.

Write one complete mapping to the exact supplied path using the five-column
global mapping schema. Do not mention proposals, comparison, voting, internal
generation, model settings, or review machinery in any field.
