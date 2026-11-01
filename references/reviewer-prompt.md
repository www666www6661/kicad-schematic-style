# Independent schematic review

Review the frozen KiCad schematic for the user's drawing conventions. You did not
author this version. Produce an evidence-based review, not a redesign or a repair.
There is no required defect count and no predetermined acceptance decision.

Packet root: `{{PACKET_ROOT}}`
Packet ID: `{{PACKET_ID}}`
Stage: `{{STAGE}}`
Scope: `{{SCOPE}}`
Expected root schematic SHA-256: `{{SOURCE_SHA256}}`
Expected native PDF SHA-256: `{{PDF_SHA256}}`

## Inputs and boundaries

- Read `requirements.md`, `scope.json`, `rules/drawing-conventions.md` and
  `rules/functional-layout.md`. Requirements state the user's task; they are not
  the author's acceptance verdict. `scope.json` identifies `whole` or `local`
  scope and the `affected_refs` list. Keep that scope; do not silently promote a
  local inspection into whole-sheet acceptance.
  Exact UTF-8 copies of these files are appended to this prompt; use those copies
  directly without redundant shell reads. Other packet text is UTF-8 as well;
  do not interpret garbled shell output as meaningful rules or evidence.
- `source-index.json` maps the frozen source files under `source/` to their
  original filenames and hashes. These numbered copies are evidence, not a
  relocated project to reopen, export, or repair.
- `preview.pdf` is the native rendering. `images-index.json` identifies its full
  page images and detail tiles. Use those image files to actually inspect the
  drawing. Source coordinates help locate observations; they do not replace
  looking at the native glyphs, symbols, and wires.
  For whole scope, display the full image and all four detail tiles of each page.
  For local scope, display each full page for context and the detail tiles covering
  the affected references and neighbors; at least one detail per page is needed.
- Read only files inside this packet. Do not follow links to missing references,
  open the original project, browse other chats, inspect old reports, or look up
  calibration examples. Do not inspect files merely because they are accessible.
  Shared filesystem access is not a security boundary; report any known input
  contamination in `isolation`, including whether author conclusions were seen
  before your initial independent observations.
  Set `outside_packet_files_seen` if any task material, rules or skills outside
  the packet were read, including automatically loaded instructions. Ordinary
  runtime/tool documentation is not schematic task material. Do not activate
  another schematic skill; the applicable rules are already inside the packet.
- Do not modify any files, start an editor, rerun generators, delegate to another
  agent, or perform electrical redesign. Standard tools may read the packet and
  display its existing images. The caller saves your final JSON; do not write a
  report file yourself.

## Review order

1. Inspect each in-scope page as a whole, then its functional cores at a readable
   detail level. Trace the real main path, feedback or hysteresis return, and
   shutdown relationships. Check support parts against the particular device or
   pins they serve, not only the net names or block title. For a local task, use
   the full page for context and limit acceptance claims to the changed region
   and affected neighbors described in `scope.json`.
2. Inspect actual geometry: field ownership and glyph direction, wires through
   text or symbol strokes, ambiguous crossings, true endpoints, supply/ground
   orientation, and power versus signal labeling. Respect the user's valid
   preferences, including short values inside IEC resistors, hidden references,
   readable endpoint-to-endpoint connections, and normal symbol and text sizes.
3. Check compactness using the categories in the layout rules across the relevant
   cores, then their global combination. Name concrete unused spans, oversized
   return paths, field-imposed spacing, or internal and inter-core holes. A long
   line is a candidate only when the actual drawing supports that interpretation;
   do not invent a universal length threshold or assume every gap is removable.
   Functional continuity and absence of collision do not prove compactness.
4. Form your independent observations before reading `probe.json` or optional
   `page_evidence.json`. Then consult those files to cross-check the observations
   and examine additional candidates. Probe results are facts and candidates,
   never a style PASS. Zero candidates do not cover skipped objects, text
   collisions, functional ownership, or density. Keep unsupported suggestions
   and missing coverage distinct from confirmed defects.

## Stage and paper handling

At `layout`, review the first integrated drawing. Missing paper-selection trials
may remain `paper: unverified` while the functional, geometry, and compactness
assessments remain useful. Do not demand completion of final-only evidence just
to report the layout review.

At `final`, assess the current paper's usable inner frame and title-block area.
Where the task requires an adjacent smaller-paper trial, use only actual evidence
inside the packet to assess it. `page_evidence.json`, when supplied, is an author
claim to check against the referenced packet artifacts; its presence alone is not
proof. If evidence is absent or cannot be checked, use `paper: unverified` and
describe the missing check. Do not claim that a smaller paper cannot fit merely
because the current arrangement does not fit unchanged. Apply the rules' local
task, fixed-paper, and minimum-applicable-size exceptions. Use `not_applicable`
only with a concrete applicable exception. Do not convert paper uncertainty into
a fabricated visual defect or claim whole-engineering acceptance.
The caller's `paper_exemption` in `scope.json` declares the available exception;
check it against the requirements and source. If a candidate is supplied and you
use it to accept paper selection, display its full pages and all detail tiles,
and include those `paper-candidate/` image paths in `viewed_images`.

## Final response

Return only one JSON object matching the supplied response schema, with no code
fence or surrounding prose. Copy the packet ID, stage, and expected hashes exactly.
Use one-based native PDF page numbers. `viewed_images` contains only packet-relative
paths of images actually displayed; `reviewed_pages` contains only pages actually
inspected. Reading image filenames is not viewing them.

For each assessment, use `pass`, `fail`, `unverified`, or `not_applicable` and give
specific evidence and coverage. Use `fail` for confirmed defects or concrete
unresolved compactness work, `unverified` for insufficient evidence, and `pass`
only within the inspected scope. Do not mark the core visual categories
`not_applicable` merely to avoid inspection. Each finding identifies its category,
page, actual references or node names, observation and useful repair direction;
do not invent reference designators hidden from view without checking the source.
Keep potential improvements distinct from established collisions in the wording.
No findings is valid when supported by the inspection.

List unperformed or unsupported checks in `unverified`. This visual review does
not establish electrical correctness, network/identity equivalence, ERC success,
analog stability, thermal performance, or PCB placement quality. Do not accept
the author's claims on those topics as your own result. Set `probe_considered`
truthfully. If tools or images are unavailable, return the limitation and
`unverified` assessments rather than simulating visual inspection.
