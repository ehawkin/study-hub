---
name: download-readings
description: >
  Fetch a course's core reading list from its course site (KEATS, Moodle,
  Canvas) using Chrome: find the list, extract each paper with its week, and
  download the PDFs the student's own enrolment serves, filed so Study Hub and
  the core-readings skill can use them. Use when someone says "get my core
  readings", "download the reading list", or after download-keats when the
  course names set readings.
---

# Downloading a course's core readings

You are working in the student's own signed-in browser, on a list their
enrolment entitles them to read. Three rules are absolute, the same three the
other download skills carry:

1. **They sign in themselves.** If a page asks for credentials, stop and hand
   the browser to them; never type, store or read out a password.
2. **Never bypass an access check.** A paper their enrolment does not serve is
   left as a reference with a DOI link, honestly labelled. No mirrors, no
   "free PDF" sites.
3. **What you download is theirs to study, never to redistribute.** Files land
   in their course folder and stay there.

## Finding the list

- Look in the course navigation for anything named **Reading list**, **Module
  Reading Lists**, **Core reading**, or similar. 🔴 Sidebar links lie: on one
  real course the link labelled "Reading List" opened the webinars section.
  Trust where a link actually goes, not its label.
- Moodle's "Tab display" plugin (`mod/tab/view.php`) is a common shape: one
  tab per week. **Every pane is already in the DOM**, so read them all with
  one JavaScript pass over `.tab-pane` / `.tabcontent` instead of clicking
  through tabs.
- The headings are often links, and on KEATS those links are usually
  `pluginfile.php/...pdf`: **the reading list carries its own files.** Collect
  `{week, label, citation, url}` for every entry before downloading anything.

## Downloading

- Fetch through the page's own session (`credentials: 'include'`), one file
  at a time; a burst of downloads makes Chrome ask about "multiple downloads",
  and that dialog blocks everything.
- Verify each file really is a PDF (starts `%PDF`) and is not tiny; a login
  page saved as `.pdf` is the classic silent failure.
- Name each file `W<n> - <Author> <Year> (<original file name>).pdf` and put
  it in `courses/<CODE>/readings/` inside the Study Hub courses folder. The
  week prefix is data: Study Hub and the summaries carry it forward. The
  parenthetical keeps the file findable against the course site's own listing
  (EH's design, 2026-08-23); drop it only when the original name is already
  the standard form.

## When the list has citations but no files

Resolve each citation to a DOI through CrossRef (search title plus year, then
**check the returned record actually is the paper** — a surname match is not
an identification, and a DOI is never guessed). Record the DOI link. If the
publisher copy is open access, that link is enough; do not chase paywalled
copies.

## Afterwards, in the same session

1. Run every downloaded PDF through the **pdf-fix** step (see the
   download-keats skill): slides and scans often have rotated pages and poor
   OCR, and fixing that before summarising improves everything downstream.
2. Offer to run the **core-readings** skill next, so each paper gets its
   summary in Study Hub, carrying the week you recorded.
3. Say what you actually got: how many files, how many left as DOI links and
   why, and where they landed.
