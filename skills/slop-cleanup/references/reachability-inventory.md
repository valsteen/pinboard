# Reachability inventory

Use this reference for a repository-wide cleanup whose stopping condition requires accounting for production definitions. A mechanical inventory is a completeness aid and candidate generator. It cannot prove semantic reachability in a dynamic program, and a textual reference does not prove a real producer or consumer.

## Keep compact evidence

Maintain a private scratch ledger with:

- supported roots and the authority that declares them;
- dynamic mechanisms and how each is enumerated;
- inventory command or analyzer, scope, completion state, and result count;
- closed-family inventory, member count, producer/consumer disposition, and unresolved members;
- one atom row per closed-family member with an exact production producer selector, production consumer selector, boundary or compatibility reason, and disposition;
- candidate selector, references, producer, consumer, persisted or protocol responsibility, provenance, disposition, and next observation;
- retained exceptions and their exact reopen condition;
- names and explanatory surfaces affected by each cleanup family.

Keep paths, symbols, counts, and short conclusions. Do not paste full source files or unfiltered analyzer output into context. Work through one coherent candidate family at a time. Re-run only affected inventories during recursion, then run the complete set once at the fixed point.

For a whole-repository pass, declare roots that span every supported runtime and installed entry point, agent skill and metadata surface, product and contributor document, generated projection and generator, development tool and configuration, package and release surface, dependency and lockfile, and CI assurance path. Declare tests, fixtures, examples, and prototypes separately as evidence roots rather than production. Record that root list in the private ledger and use the identical ordered `--production-root` and `--test-root` arguments for generic and enriched runs; differing input digests invalidate the comparison.

For each cleanup family, enumerate names in identifiers, route and command labels, public fields, protocol and serializer tags, database objects, history, generated presentation, documentation, metadata, defaults, and versions. Keep a name when a current external, persisted, or compatibility consumer requires it; otherwise remove archaeological wording with the unsupported family.

## Run the bundled inventory

Resolve `../scripts/inventory.py` relative to this reference. Run it through the repository-owned environment command when the repository specifies one; otherwise use the Python interpreter provided by the plugin host. Do not bypass a repository's locked environment or invoke a prohibited ambient interpreter merely because the helper is installed outside the checkout. Give it the repository and exact production and test roots. Do not read the helper implementation during an ordinary audit. Inspect its source only when execution fails, its receipt contradicts this documented contract, or the task is changing the helper itself.

Write each full report once to private scratch space with `--output`. The command prints a compact receipt containing the input digest, summary, and candidate counts. Query the saved JSON for individual candidate families instead of printing the complete report or rerunning the helper to extract another section.

The report's `semantic_disposition` contract names the eight semantic candidate categories, their mechanical candidate sources when available, and the required semantic fallback. It also names the only terminal dispositions. The generic and Python-AST reports must carry identical ordered roots, input digest, and semantic disposition contract. A candidate collection is not a defect list.

For a repository containing production Python, run the same tracked revision and roots twice:

```text
python inventory.py --repository <repo> --production-root <root> --test-root <root> --mode generic --output <private-scratch>/generic.json
python inventory.py --repository <repo> --production-root <root> --test-root <root> --mode python-ast --output <private-scratch>/python-ast.json
```

Run each mode once per input digest. Require the two reports to have the same `input_digest` and the recorded whole-repository root declaration. Use the generic run as the portability baseline and the AST run as additional Python evidence. Compare candidate selectors and closed-family atoms in both directions. When evaluating a language-portable structural fallback, also compare its complete frozen construct universe with the AST counts and selectors category by category; any mismatch invalidates the fallback receipt even when its candidate groups agree. Investigate useful generic findings that disappear under enrichment, and record which additional candidates depend on AST precision. Do not silently replace the generic run with the richer one.

In both modes, inspect `singleton_closed_families` after every member removal and record the external or persisted boundary contract for any retained singleton. In Python AST mode, begin the navigation pass with `trivial_callable_bodies`, `equivalent_match_arms`, `exhaustive_passthrough_matches`, and `duplicated_match_structures`. These are syntax candidates, not deletion decisions. Verify whether each site owns validation, policy, an independently meaningful boundary, or another real responsibility before changing it.

Coverage is invariant even when the efficient analyzer path is unavailable. For a repository without production Python, or for a language-specific category not covered by an existing analyzer, use an existing parser, analyzer, tokenizer, or disposable lexical sweep that recognizes every in-scope construct in the repository's actual language. Reconcile its independently counted construct universe as inspected, explicitly excluded, or failed, with zero unexplained extraction failures. The fallback may cost more inspection, but it must produce the same category-by-category candidate or zero-result receipts. Never translate “unsupported by this helper” into “not required for this language.”

The generic pass deliberately uses conservative text structure rather than language parsers. Its current coverage receipt names the exact recognized forms: declaration-like symbols in Go, Kotlin, Python, Rust, and TypeScript; common enum shapes where available; SQLite `CREATE` objects; literal `CHECK ... IN (...)` vocabularies even when SQL is embedded in another source file; tracked text; and common assets. Pair it with analyzers already declared by the repository. Add no parser or permanent dependency merely to make another language resemble Python.

Read every coverage receipt. `complete` means the named mechanical category completed for the given inputs, `partial` means every matching file was visited but the extraction is heuristic, and `unsupported` leaves a mandatory semantic or repository-specific pass. The helper intentionally leaves producer/consumer meaning, dynamic reachability, repeated traversals, duplicated dispatch, and protocol overlap unsupported. For every partial or unsupported category relevant to an approved removal, add a ledger row naming the inspection method or existing analyzer, exact scope, result count or selectors, and disposition. “Inspected,” “checked manually,” or “all categories covered” without those fields is not a receipt. A run cannot establish the cleanup fixed point while a required category lacks this evidence.

For the semantic categories, the repository-specific pass must record exact selectors and apply the report's disposition method. Cover dynamic registration through the repository's actual entry-point and manifest owners. Cover stored and wire vocabularies through their actual codecs and readers. Cover compatibility through retained data and independent-consumer evidence. Cover speculative obligations through accepted authority or an observed supported-path consequence. A consequential unresolved selector blocks terminal acceptance rather than becoming an automatic retained exception.

## Generic method

1. Enumerate tracked production files and package contents separately from tests, examples, generated files, and prototypes.
2. Enumerate production roots from product documentation, package metadata, executables, framework or plugin registration, supported public APIs, protocols, and persisted-data readers.
3. Enumerate definitions with the best existing language-aware tool. Use the bundled generic inventory as a cross-language baseline rather than as a parser-equivalent proof. Record stable selectors rather than definition bodies.
4. Inventory the atoms of each closed production family separately from definitions: enum members, tagged-union variants, command routes, serializer tags, schema alternatives, error codes, and other finite vocabularies. For each atom, fill the producer/consumer row with concrete production selectors. A declaration, type reference, decoder, schema constraint, conversion table, test fixture, or stored row is not by itself a producer; a reader or rejection path is not by itself a consumer. Group atoms only when the same named mechanism genuinely produces and consumes every grouped member. A reference to the containing type does not account for all of its members.
5. Enumerate static references, imports, exports, registrations, and serialized names. Treat ambiguous name or attribute matches as leads to inspect.
6. Enumerate dynamic mechanisms explicitly: reflection, dependency injection, generated code, runtime registries, decorators, module hooks, configuration-selected names, plugin discovery, and deserialization.
7. Reconcile every production definition and every closed-family atom to a root, verified dynamic mechanism, compatibility responsibility, cleanup candidate, or exact retained exception. Verify each candidate's real producer and consumer before deciding its disposition.

Prefer analyzers already declared by the repository. Do not add a permanent dependency merely to perform one audit. A temporary standard-library scanner or already-available analyzer is appropriate when it replaces repeated manual searches.

## Python

Start with package metadata and the installed shape: inspect `pyproject.toml` scripts and entry points, `__main__.py`, package inclusion rules, documented imports, and framework or plugin registrations. Do not assume that every importable module is a supported library API.

Use the repository's Ruff and type checker results first. They catch unused imports, undefined names, impossible type paths, and related residue, but they do not detect all unused functions, classes, methods, enum members, or fields.

For a compact definition inventory, use a temporary standard-library `ast` scanner over production `.py` files to list module-level functions, async functions, classes, assignments that define public registries or constants, and class members when the cleanup family requires them. Pair it with import and name-reference counts, then inspect ambiguous attribute access manually. If the project already uses Vulture, run it as a candidate generator; do not accept its report without checking dynamic use and supported roots.

Run the AST-backed closed-family inventory. It enumerates `Enum` members and their literal values, explicit union aliases, multi-value `Literal` annotations, and tags of explicit `msgspec.Struct` unions. Add command routes, schema alternatives, error-code vocabularies, and serializer forms that use another representation. Start the semantic pass with `closed_atoms_without_apparent_non_declaration_production_use`, then inspect the remaining low-reference members and complete their producer/consumer rows. Boundary replication can explain why an atom appears in several declarations without proving that the product can create or exercise it. Do not infer that all members are live because the containing type is referenced, and do not infer that a low textual count is dead when decoding, persistence, presentation, or generated dispatch consumes it indirectly.

Account explicitly for Python mechanisms that evade ordinary reference searches, including packaging entry points, `importlib`, module or class `__getattr__`, decorators that register callables, `singledispatch`, framework discovery, serializer tags, string-named callbacks, and registries populated at import time.

Coverage or tracing from representative installed commands is useful positive evidence that a path executes. Lack of coverage is only a cleanup lead because supported paths may be input-, platform-, or state-dependent. Conversely, tests executing a symbol do not make it production-reachable.

At the fixed point, report the definition count and closed-family atom count separately, how many are rooted dynamically, how many are retained for compatibility, how many exact exceptions remain, and whether the final inventory produced any new candidates. Do not claim that Python reachability was mechanically proven.
