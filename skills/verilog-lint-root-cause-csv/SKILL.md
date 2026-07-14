---
name: verilog-lint-root-cause-csv
description: Use for cross-level Verilog lint root-cause analysis from prepared RTL, slices, and filelist inputs.
---

# Verilog Lint Root-Cause CSV

## Input

Require these three prepared design-evidence paths:

- the `rtl/` source directory;
- the `slices/` directory;
- the project `filelist.f`.

Also require one exact draft CSV output path from the outer workflow. Treat it
as an output target, not as design evidence.

Do not read the original source archive, original lint CSV, hierarchy work
directory, or slice policy. Do not preprocess inputs, classify modules, build
slices, sort, validate, publish, or create timestamped reports. The outer
workflow owns those operations.

## Analyze

Read `slices/coverage.json` first. Then process the exclusive lint scopes in
this order without merging their `lint.csv` files:

```text
level1 -> level2 -> level3 -> level4 -> isolated
```

For each scope:

- use `context.json` for module source paths, parameters, ports, and child
  instances;
- use `lint.csv` for exclusively owned alerts;
- use the project `filelist.f` for compile order and macros;
- use `slices/hierarchy_tree.txt` for active instance context;
- for every lint row, open `rtl/<source_file>` at `source_line` and inspect the
  relevant enclosing construct before assigning its root cause, grouping, fix,
  or false-positive status;
- never make any decision from `message_id` or `contents` alone; if the
  corresponding source and relevant context have not been inspected, do not
  finalize that row;
- treat a semicolon-separated `hierarchy` field as equivalent instances of one
  source warning, not as multiple leaf violations;
- analyze isolated rows against their source and child instances; absence from
  the active hierarchy alone does not prove a false positive.

After completing each scope, write its analyzed rows into the one supplied
draft while retaining all rows from earlier scopes. Do not create separate
per-scope reports. After all five scopes are present, consolidate that same
draft into the global result: merge cross-level rows when they share the same
defect mechanism and concrete repair strategy, make their category fields
consistent, and keep every leaf row exactly once.

Treat a lint finding as a false positive when the reported code is intentional,
conforms to the project design intent, and requires no RTL change. A rule may
correctly recognize a code pattern and still be a false positive when it cannot
account for that design intent. If the RTL requires a change, use a normal root;
if the design intent cannot be confirmed, continue the analysis instead of
marking the finding as a false positive.

Maintain one global defect-class view across every scope. Assign the same
`root_id` when findings share the same underlying defect mechanism and can be
resolved by the same concrete repair strategy:

- findings do not need to occur at the same source location or be cleared by
  one source edit;
- do not group findings only because they share a level, lint rule, or generic
  repair wording;
- keep findings separate when their defect mechanisms or required repairs
  differ, even if they share the same lint rule;
- allow findings from different rules or levels to share one root when both
  their defect mechanism and concrete repair strategy match.

### Derived roots

Use `parent_root_id` only to record direct causal dependence between two
distinct normal roots. Treat a root as derived only when source, hierarchy, or
signal-flow evidence shows that the parent defect causes or enables the child
defect, while the child still has a different defect mechanism or requires a
different concrete repair.

Apply these rules:

- if two findings share both mechanism and repair, merge them under one
  `root_id` instead of creating a parent-child pair;
- if repairing the parent completely removes a downstream finding and no
  separate child repair remains, assign that finding to the parent root;
- make every child point to its nearest direct parent, not a remote ancestor;
- require both parent and child to be normal roots backed by at least one lint
  row; never invent an empty parent root and never attach `误报` to a parent;
- when several roots are merely related, correlated, or in the same module,
  level, rule, or signal path, keep `parent_root_id=/` unless one direct causal
  direction is supported;
- when more than one possible parent exists but no single direct parent can be
  established, use `/` rather than guessing;
- allow a confirmed parent-child relationship to cross slice levels, and
  resolve it during global consolidation;
- state the child's local mechanism and its causal link to the parent in
  `root_note`; state the child-specific repair and any required repair order in
  `fix_suggestion`.

For example, violations caused directly by one combinationally generated clock
and cleared by replacing it with a clock enable belong to one root. Create a
derived child only if a separate downstream clock-generation construct is
caused by that root and still requires its own RTL change; point that child to
the generated-clock root.

Map leaf fields directly from each scope `lint.csv`:

```text
leaf_violation_id   = vio_id
leaf_violation_note = message_id:contents
```

Preserve `message_id` and `contents` exactly.

## Write the draft

Write or revise only the exact draft path supplied by the outer workflow. Keep
the same path throughout all revisions. Write exactly these columns in order:

```text
root_id,root_note,fix_suggestion,root_file_path,root_file_start,root_file_end,parent_root_id,leaf_violation_id,leaf_violation_note
```

Apply these rules:

- write one output row for every lint row and exactly one leaf ID per row;
- use stable normal IDs such as `root_001`;
- repeat identical `root_note`, `fix_suggestion`, and `parent_root_id` values on
  every row sharing a normal root ID;
- write `root_note` and `fix_suggestion` in concise Chinese;
- use the exact POSIX path relative to the supplied `rtl/` directory for
  `root_file_path` (for example, `core/a/foo.v`) and record the concrete defect
  occurrence for that leaf; never collapse nested paths to a basename;
- use 1-based inclusive line bounds for that occurrence that do not exceed the
  source file; rows sharing a root ID may have different occurrence paths and
  ranges;
- use `/` for an independent root's `parent_root_id`;
- for a false positive, use `root_id=误报`, explain in `root_note` why the
  code is intentional and needs no RTL change, and use `/` for
  `fix_suggestion` and `parent_root_id`;
- preserve commas, quotes, and newlines with standard CSV quoting;
- do not add columns or combine leaf IDs.

## Review

Before finishing, perform a second source-based pass over every draft row. For
each row, locate its original entry in the corresponding scope `lint.csv`,
reopen `rtl/<source_file>` at `source_line`, inspect the relevant enclosing
construct, and reopen the recorded `root_file_path` and root range when they
differ from the leaf location. Never approve a row from the lint text or draft
text alone. Confirm:

- every `vio_id` occurs exactly once and both leaf fields are exact;
- every normal root groups one defect mechanism and one concrete repair
  strategy, while each row records its own valid occurrence location;
- every derived root has source-backed direct causality, remains distinct from
  its parent under the grouping rules, and points to the nearest existing
  parent;
- every false-positive decision is supported by reopened source and context
  showing that the code is intentional, conforms to project design intent, and
  requires no RTL change;
- repeated normal roots have consistent category fields and every parent root
  exists;
- the schema is unchanged and authored natural-language fields are Chinese.

Do not finish after the first draft. Revise the same draft path until this
second-pass review is complete, then return control to the outer workflow.
