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
  open the original project, browse other chats, inspect outside reports, or look up
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

1. Inspect all in-scope pages together, then each page and its cores at a readable
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
   For in-scope new or changed device/symbol/footprint selections, compare any
   original terminal and footprint-pad evidence supplied in `requirements.md`
   with the source symbol, using the terminal-matching rule. Do not infer package
   completeness from signal count, a generic connector name, ERC, or matching
   netlists. A confirmed omitted terminal is a `functional_reading` finding;
   name the reference and source evidence. If the applicable part/package data
   is missing, record the specific unresolved mapping in `unverified` without
   inventing a defect or reading outside the packet. Do not presume mounting
   or shield pins connect to ground, and do not repeat unrelated part selection
   for a layout-only edit. This check does not certify the whole PCB footprint.
3. Check compactness using the categories in the layout rules across the relevant
   cores, then their global combination. First inspect within-core endpoint spans,
   returns, branch pitch and fields. Separately return to the full page and examine
   its largest internal gaps between groups: empty row/column bands, usable notches,
   and small groups spread around a tall core. Consider whether a complete group
   could move closer, change rows or use a neighboring notch while keeping its
   role clear. Name the actual groups, direction and obstacle or repair. Titles,
   sheet links and mechanical declarations do not justify fixed large slots.
   Record these observations in `space_checks`; cover all relevant cores, merge
   repeated causes, and do not stop after finding or closing one local long wire.
   For whole scope, each page needs both `within_core` and `between_cores` records.
   For local scope, each page needs `within_core` records for the affected region;
   include `between_cores` when affected. Each record names source references,
   nodes or sheet names in `anchors`, the observed space and either a useful action
   (`change_needed`), a visible reason to retain it (`retained`), or the missing
   evidence (`unverified`). When no problem exists, describe the largest inspected
   space and its real constraint; a single-core page can explain why there is no
   inter-core packing work. There is no required number of defects.
   Unused paper beyond the content and reserved title-block space are not defects.
   A long line is a candidate only when the actual drawing supports that
   interpretation; do not invent a universal length or density threshold.
   Functional continuity and absence of collision do not prove compactness.
   For a whole-project review with multiple pages, also assess `pagination` and
   record at least one `across_sheets` space observation. Identify the actual
   sheets and cores in `anchors`; use the page containing the main candidate as
   the record's page. Examine whether small complete cores on separate sheets
   could join neighboring content, and whether sheet separation has a real
   reuse, reading-complexity or explicit user requirement. Functional grouping
   alone does not require separate sheets. The author's current page count,
   generator boundaries and agent assignments are not user constraints for a
   newly created drawing. Do not accept pagination merely because every page
   is locally compact or A5. Existing local edits keep their authorized scope.
   A visible consolidation opportunity is a candidate requiring evaluation,
   not proof that everything fits on one sheet. Report concrete unresolved
   partition work or insufficient evidence without prescribing a fixed size.
4. Form your independent observations before reading `probe.json` or optional
   `page_evidence.json`. Then consult those files to cross-check the observations
   and examine additional candidates. Probe results are facts and candidates,
   never a style PASS. Zero candidates do not cover skipped objects, text
   collisions, functional ownership, or density. Keep unsupported suggestions
   and missing coverage distinct from confirmed defects.
5. If `previous-review.json` exists, read it only after forming the independent
   observations above. It is the previous reviewer's report, not the author's
   repair claim. Recheck every previous finding against the current images and
   source. Return one `rechecks` entry per prior finding ID: `resolved`, `open`,
   `not_applicable`, or `unverified`, with current evidence. `not_applicable` needs
   a concrete changed condition or a supported correction of the prior finding;
   do not use it merely because repair is difficult. Keep still-open and
   unverified prior findings in the current `findings` with their original ID
   and category; explain any uncertainty in their observation, so a later round
   does not lose them. Add new findings for regressions. Do not limit the current
   inspection to the old defect list.
   Without that packet file, `rechecks` is empty. Reading this frozen report at
   this point is the planned follow-up, not first-observation contamination.

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

For final whole-scope review without a paper exemption, a paper pass also needs
one `space_checks` entry at level `paper_trial` for each current page. Name the
corresponding candidate page and cores in `anchors`. Assess the quality of the
trial itself: does it use the smaller inner frame and available notches, and
does it address the main obstruction with a changed arrangement or local shape?
An unchanged large-sheet row structure, unresolved overlaps, or groups parked
outside the page are unfinished layout work, not a demonstrated size constraint.
Use `change_needed` with a paper finding for concrete unfinished trial work,
`unverified` for missing evidence, or `retained` when the observed trial is
sufficient and a specific readable-layout constraint justifies the selection.
Do not require exhaustive packing or invent a mandatory paper size. A fitted,
acceptable smaller candidate should be adopted, not used to justify the larger
sheet. Inspect the candidate source and its indexed probes as well as the PDF:
clipped objects can disappear from the rendered image. Probe bounds are screening
facts, not complete field extents or proof that another arrangement is impossible.

Judge the current drawing's local and global compactness independently of paper
selection. A5 or a fixed-paper exception does not waive either check. A failed
smaller-paper attempt does not justify removable gaps on the current page. Missing
paper evidence belongs in the paper assessment; do not make it the sole reason
for an unverified compactness assessment after actually completing those checks.
Pagination is required at both stages for whole multi-page reviews. Its
`not_applicable` status is allowed only for a single native PDF page or local
scope; an explicitly retained multi-page hierarchy still needs evidence that
it matches the user's requirements. The A5 paper exception does not exempt
pagination. A pagination pass does not prove globally minimal page count.

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

Use schema version 2. A `change_needed` space check requires a failed assessment
and a finding on that page: `compactness` for `within_core`/`between_cores`,
`pagination` for `across_sheets`, or `paper` for `paper_trial`. Unverified space checks block acceptance. Every
failed assessment needs at least one finding in its category. Finding IDs must
be unique. An `open` recheck requires the same finding ID and category in the
current findings, with that category assessed `fail`; an unverified recheck blocks
acceptance. Do not list a finding as both resolved and still active. Concrete
evidence is a reviewer's observation, not a measurement
or proof supplied by the gate validator.

List unperformed or unsupported checks in `unverified`. This visual review does
not establish electrical correctness, network/identity equivalence, ERC success,
analog stability, thermal performance, or PCB placement quality. Do not accept
the author's claims on those topics as your own result. Set `probe_considered`
truthfully. If tools or images are unavailable, return the limitation and
`unverified` assessments rather than simulating visual inspection.
