# Triage Rubric

Use this rubric after the detector has already produced a filtered report.

## Likely Real Defect

- The root is a parent-module named wire or port that was made constant in the middle of a hierarchy.
- The same root pollutes multiple child modules or multiple internal control paths.
- The polluted targets are control signals, not only data constants.
- The removed items are concentrated around logic that should normally depend on runtime state, debug state, exception state, branch state, valid/ready flow, or protocol handshakes.
- The source code does not contain a clear design comment or configuration rationale for the tie-off.
- The root looks newly introduced or inconsistent with the surrounding module interface intent.

## Likely Design-Intended Constant

- The root is a well-known architectural/configuration constant such as disabled feature flags, fixed privilege mode, or permanent protocol tie-off.
- The source code or comments explicitly say the feature is unsupported, fixed, or intentionally tied off.
- The polluted logic is exactly the logic that implements the disabled feature.
- The signal naming and nearby code clearly indicate a wrapper tie-off or a feature-off configuration path.

## High-Signal Questions

- Is this root introduced in a parent module instead of originating from an intentional top-level configuration?
- Does the same root affect more than one child module or more than one logical branch?
- If the root were restored to non-constant, would the removed logic become behaviorally meaningful again?
- Does the local source code make the tie-off look intentional, documented, and configuration-driven?

## Recommended Evidence Set

For each likely real defect, collect:

- The parent-module named root from the report.
- One or two representative polluted child-module signals.
- One or two representative removed local cells with source locations.
- The source lines around the root and around one polluted module location.
- A short explanation of why this looks unintended rather than configuration-driven.
