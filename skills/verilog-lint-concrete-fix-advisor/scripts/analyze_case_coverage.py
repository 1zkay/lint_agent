#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


CASE_RE = re.compile(r"\bcase\s*(?:\([^)]*\)|[^\n;]+)", re.IGNORECASE)
CASE_VALUE_RE = re.compile(
    r"(?m)^\s*([^:\n]+?)\s*:\s*(?!//).*?$"
)
BASED_LITERAL_RE = re.compile(r"(?i)^\s*(\d+)?\s*'\s*([bhd])\s*([0-9a-f_xz?]+)\s*$")
DECL_RE_TEMPLATE = r"\b(?:input|output|inout|wire|reg|logic)\b(?:\s+(?:signed|unsigned))?\s*(?:\[(\d+)\s*:\s*(\d+)\])?\s+[^;]*\b{}\b"


@dataclass
class CaseCoverage:
    start_line: int
    end_line: int
    selector: str
    inferred_width: int | None
    has_default: bool
    explicit_values: list[str] = field(default_factory=list)
    missing_values: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def configure_stdio_utf8() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def line_number_at(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def find_case_blocks(text: str) -> list[tuple[int, int, str, str]]:
    blocks: list[tuple[int, int, str, str]] = []
    pos = 0
    while True:
        match = re.search(r"\bcase\s*\(([^)]*)\)", text[pos:], flags=re.IGNORECASE | re.DOTALL)
        if not match:
            break
        start = pos + match.start()
        selector = match.group(1).strip()
        body_start = pos + match.end()
        depth = 1
        cursor = body_start
        token_re = re.compile(r"\b(case|endcase)\b", re.IGNORECASE)
        end = -1
        for token in token_re.finditer(text, body_start):
            word = token.group(1).lower()
            if word == "case":
                depth += 1
            elif word == "endcase":
                depth -= 1
                if depth == 0:
                    end = token.end()
                    body = text[body_start:token.start()]
                    blocks.append((start, end, selector, body))
                    cursor = end
                    break
        if end < 0:
            break
        pos = cursor
    return blocks


def literal_to_int(token: str) -> tuple[int | None, int | None]:
    token = token.strip()
    match = BASED_LITERAL_RE.match(token)
    if match:
        width = int(match.group(1)) if match.group(1) else None
        base = match.group(2).lower()
        digits = match.group(3).replace("_", "").lower()
        if any(ch in digits for ch in "xz?"):
            return width, None
        radix = {"b": 2, "h": 16, "d": 10}[base]
        return width, int(digits, radix)
    if re.fullmatch(r"\d+", token):
        return None, int(token)
    return None, None


def format_binary(value: int, width: int) -> str:
    return f"{width}'b{value:0{width}b}"


def infer_selector_width(source: str, selector: str, literal_widths: list[int]) -> tuple[int | None, list[str]]:
    notes: list[str] = []
    if literal_widths:
        widths = sorted(set(literal_widths))
        if len(widths) == 1:
            return widths[0], notes
        notes.append(f"case item widths differ: {widths}; using max width")
        return max(widths), notes

    simple = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", selector.strip())
    if simple:
        name = re.escape(selector.strip())
        match = re.search(DECL_RE_TEMPLATE.format(name), source)
        if match and match.group(1) is not None:
            msb = int(match.group(1))
            lsb = int(match.group(2))
            return abs(msb - lsb) + 1, notes

    notes.append("could not infer selector width reliably")
    return None, notes


def analyze_case(source: str, cleaned: str, block: tuple[int, int, str, str]) -> CaseCoverage:
    start, end, selector, body = block
    start_line = line_number_at(source, start)
    end_line = line_number_at(source, end)

    raw_items = [m.group(1).strip() for m in CASE_VALUE_RE.finditer(body)]
    has_default = any(item.lower() == "default" for item in raw_items)

    explicit_values: list[str] = []
    literal_widths: list[int] = []
    int_values: set[int] = set()
    notes: list[str] = []

    for item in raw_items:
        if item.lower() == "default":
            continue
        for part in [p.strip() for p in item.split(",") if p.strip()]:
            width, value = literal_to_int(part)
            if width is not None:
                literal_widths.append(width)
            if value is None:
                notes.append(f"non-constant or wildcard case item skipped: {part}")
                continue
            int_values.add(value)
            explicit_values.append(part)

    width, width_notes = infer_selector_width(cleaned, selector, literal_widths)
    notes.extend(width_notes)

    missing_values: list[str] = []
    if width is not None and width <= 16 and not has_default:
        all_values = set(range(2**width))
        for value in sorted(all_values - int_values):
            missing_values.append(format_binary(value, width))
    elif has_default:
        notes.append("default branch exists; explicit value coverage is not exhaustive but case has a fallback")
    elif width is not None and width > 16:
        notes.append("selector width is too large for exhaustive enumeration")

    return CaseCoverage(
        start_line=start_line,
        end_line=end_line,
        selector=selector,
        inferred_width=width,
        has_default=has_default,
        explicit_values=explicit_values,
        missing_values=missing_values,
        notes=notes,
    )


def main() -> int:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(description="Analyze Verilog case coverage and missing case items.")
    parser.add_argument("source_file", help="Verilog/SystemVerilog source file")
    parser.add_argument("--line", type=int, default=None, help="optional 1-based source line inside target case")
    args = parser.parse_args()

    source_path = Path(args.source_file).resolve()
    source = source_path.read_text(encoding="utf-8", errors="replace")
    cleaned = strip_comments(source)
    blocks = find_case_blocks(cleaned)
    analyses = [analyze_case(source, cleaned, block) for block in blocks]
    if args.line is not None:
        analyses = [item for item in analyses if item.start_line <= args.line <= item.end_line]

    payload = {
        "报告类型": "case 覆盖分析",
        "报告格式": "json",
        "源文件": str(source_path),
        "目标行": args.line,
        "case数量": len(analyses),
        "case分析": [asdict(item) for item in analyses],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
