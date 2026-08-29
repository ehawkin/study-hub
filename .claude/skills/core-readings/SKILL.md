---
name: core-readings
description: >
  Turn a course's core readings (scientific papers, book chapters) into
  summaries a student can skim in a minute and then open into as much detail as
  they want, linked to the readings themselves. Use this skill when someone
  mentions core readings, a reading list, "summarise my papers", "I have a
  folder of PDFs for this course", "make notes on these readings", or when they
  hand over papers and ask what to do with them. Also use it to add one reading
  to a course that already has some.
---

# Core readings

**What this is for.** A course sets papers and chapters. A student has twenty
minutes and eleven readings. The job is not to replace the reading: it is to let
them decide, in a minute, which two they must actually read this week, and to
give them enough of the other nine to follow the lecture.

🔴 **The summary is a way in, never a substitute.** Every reading gets a link to
the real thing, and the page puts that link above the summary. If you find
yourself writing something so complete that opening the paper would add nothing,
you have written the wrong thing.

## What you need from them

**Where the readings are.** A folder of PDFs is the common case. A list of DOIs
or links works too, and so does a mix. Ask which course they are for, by its code.

**Whether the files should be copied into the course.** A reading inside the
course folder is served by Study Hub and opens in one tap. A reading left where
it is gets linked by path and still opens, until they move it. Ask; do not assume.

## The shape of a summary, which is the whole job

**Two kinds, because a paper and a chapter answer different questions.** Forcing
one template on both produces a summary that is vague about each.

### Every reading has a skim layer

Three fields, and they are what the page shows without being asked:

- **`claim`**: one sentence. For a paper, the finding, stated as a claim
  somebody could disagree with. For a chapter, what the chapter is for.
  🔴 Not a topic sentence. "This paper examines the role of inflammation in
  depression" is a topic; "raised inflammatory markers precede depressive
  episodes rather than following them" is a claim. Only the second is worth
  reading twice.
- **`why`**: one or two sentences on why it is on THIS course, and where it fits.
  Name the week or the lecture if you can. This is the field a student uses to
  decide whether to read it now.
- **`takeaways`**: exactly three, occasionally four. Each one a complete
  sentence that survives being read alone. **If a takeaway needs the paper open
  to make sense, it is a note, not a takeaway.**

### A paper's deeper layer

`sections`, in this order, because it is the order in which a claim becomes
trustworthy or does not:

1. **What they did.** Design, who was in it, how many, what was measured, over
   how long. The design is what decides what the finding can mean.
2. **What they found.** The actual numbers. Effect sizes, intervals, the
   direction. **A summary with no numbers in it is a summary of the abstract.**
3. **What to be careful about.** Limitations the authors admit, limitations they
   do not, what it cannot show, and where it is contested. Never omit this
   section, and never soften it.

Add **"How it was received"** when the paper is old enough to have been argued
with, and say plainly if it has been challenged or failed to replicate.

### A chapter's deeper layer

`sections` are its **key ideas, one per section**, in the chapter's own order,
each with its explanation. A chapter has no single finding, and inventing one is
where textbook summaries go wrong. Between four and eight is usually right; more
than ten means you are transcribing rather than summarising.

### Both kinds

- **`terms`**: the vocabulary this reading introduces or uses in a particular
  way, each with a definition that stands alone. This is what a student searches
  for at 11pm.
- **`links`**: the lesson ids this reading sits beside, so the page can offer
  "read alongside". Use the course's own ids, and only ones that exist.

## Getting the facts right

🔴 **Every DOI is checked, never guessed.** Resolve it, confirm the title and
the authors match what you are holding, and if it will not resolve, leave the
field out and say so rather than shipping a link that dies. This is the same rule
the lesson writer follows and it exists because a wrong identifier is worse than
none.

**Read the reading.** Not the abstract, not the first page. The method section is
where the useful part of "what they did" lives, and the limitations are usually
in the last two paragraphs of the discussion where nobody looks.

**Say when you could not.** A scanned chapter that will not extract, a paywalled
paper: write the reference, say in `why` that the text was not available, and
leave the sections out. **A summary invented from a title is the one genuinely
harmful thing this skill can produce**, because it will be believed.

🔴 **Check whether the paper has been corrected or retracted, and record it.**
Set-reading lists are not maintained, so a paper can carry a correction, or have
been withdrawn outright, with nothing on the list to say so. **Check more than
one index**: neither of the main ones is a superset of the other, and a
corrigendum recorded by one can be entirely absent from the other. In one course
of ten set readings, one carried a published corrigendum and nothing in the
material mentioned it.

When there is one, add a `notice` to the record and the page does the rest: it
puts a caution sign on the card and its index row, and links the notice one click
away.

```json
"notice": {"kind": "corrigendum", "doi": "10.1016/j.ijpsycho.2017.02.014"}
```

`kind` is one of **corrigendum, erratum, correction, retraction, expression of
concern**, and takes a `doi` or, when the notice has none, a `url`. **It adds to
your prose, it does not replace it**: if the paper's history is worth explaining,
explain it in a section as well and both will show.

## The file

Write JSON, and show it to them before it is filed:

```json
{
  "readings": [
    {
      "id": "smith-2019",
      "kind": "paper",
      "title": "Inflammation and the onset of low mood",
      "authors": "Smith, Jones & Patel",
      "year": 2019,
      "venue": "Nature Reviews Neuroscience",
      "doi": "10.1038/s41583-019-0123-4",
      "file": "readings/smith-2019.pdf",
      "notice": {"kind": "corrigendum", "doi": "10.1038/s41583-019-0999-9"},
      "week": "3",
      "claim": "Raised inflammatory markers precede depressive episodes rather than following them.",
      "why": "It is the evidence behind week 3's causal claim, and the lecture states its conclusion without showing the design.",
      "takeaways": [
        "CRP rose an average of eight months before symptom onset in this cohort.",
        "The association held after adjusting for BMI, which is the usual confound.",
        "It is observational, so the direction of causation is argued rather than shown."
      ],
      "sections": [
        {"h": "What they did", "body": "..."},
        {"h": "What they found", "body": "..."},
        {"h": "What to be careful about", "body": "..."}
      ],
      "terms": [{"t": "CRP", "d": "C-reactive protein, a blood marker of systemic inflammation."}],
      "links": ["W3-T2-P1"]
    }
  ]
}
```

`id` may be left out and will be built from the first author and the year.
Everything except `title` and one of `claim`, `takeaways` or `sections` is
optional. **A bare reference with nothing to read is refused**, because a
bibliography is what they had before.

## Getting it into Study Hub

**Tell them to do it themselves, in the browser**, because it is the part they
will repeat:

> Open the course page and drag the file onto it.

The page works out what it is. From here, if they ask:

```
python3 server/readings.py <CODE>-readings.json --module <CODE>
```

**Re-running replaces a summary rather than duplicating it**, so a better second
pass is safe. A reading somebody has edited by hand is left alone unless
`--overwrite` says otherwise.

## Finishing

Say, plainly:

1. **How many readings, and how many you actually read in full.** The difference
   is the number that matters.
2. **Anything you could not open**, by name.
3. **Any DOI that would not resolve**, by name, and that you left it out rather
   than guessing.
4. Where the files ended up, and that the page links to them.
