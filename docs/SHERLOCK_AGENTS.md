# Sherlock: how the agents work together

Two teams share one **case board**. The **Officer team** talks to the officer and makes
sure every question gets a correct answer. The **Sherlock team** works the case in the
background. The **Question desk** sits between them, so a question is never asked twice and
every answer the officer gives reaches Sherlock. The **Ingest team** and the **CDR team**
fill the board; the **shared toolbox** finds information for both teams and never guesses.

The design, with its reasons, is in `docs/sherlock-agent-plan.html`; its section "What
changed in v6" has the current diagram of one question, one background run and one report.

**Orchestration: LangGraph** (`src/sherlocks/team/`). Each team is one compiled graph:
`team/officer_graph.py` is a chat turn, `team/sherlock_graph.py` is one background run,
`team/report_graph.py` writes the case report. The
nodes are the agents; the edges are fixed in code or follow the model's own reading - never
words matched in the officer's message. The case board is the only memory: a graph's state
just carries the turn (or the run) to its nodes. LangSmith tracing is switched off in
`team/runtime.py`: nothing about a case leaves the police network.

**Who understands language.** Every agent that has to understand what someone wrote - the
officer's message, a record, a statement - asks the model with a typed schema. Code stores,
compares structured values (dates within a tolerance, identifiers, counts, quotes that must
be in their source) and guards. The keyword rules are one node (`rules`) of the Officer
graph, used only when there is no model to read with.

```mermaid
flowchart LR
  classDef off fill:#12305a,stroke:#12305a,color:#fff
  classDef sh fill:#6a3fa0,stroke:#6a3fa0,color:#fff
  classDef desk fill:#b4232f,stroke:#b4232f,color:#fff
  classDef tool fill:#2459a8,stroke:#2459a8,color:#fff
  classDef board fill:#fff4d6,stroke:#a87a07,color:#3a2a00
  classDef ing fill:#0f7a6c,stroke:#0f7a6c,color:#fff
  classDef cdr fill:#b35a12,stroke:#b35a12,color:#fff
  O(["Officer · EN / اردو / Roman"])
  subgraph OT [Officer team]
    O1["O1 Officer agent"]:::off
    O4["O4 Case briefer"]:::off
    O2["O2 Answer checker"]:::desk
    O5["O5 Presenter"]:::off
    O3["O3 Statement recorder"]:::off
  end
  subgraph ST [Sherlock team]
    S1["S1 Sherlock"]:::sh
    S2["S2 Summarizer"]:::sh
    S4["S4 Questioner"]:::sh
    SX["S10 Fact collector · S5 Claims · S6 Contradictions ·<br/>S7 Statements · S8 Leads · S9 Skeptic"]:::sh
  end
  subgraph QD [Question and Insight desk]
    Q1["Q1 Gatekeeper"]:::desk
    Q2["Q2 Ledger"]:::desk
    Q3["Q3 Insight desk"]:::desk
  end
  TB["Toolbox: board · dossier · chat memory · report reader · CDR query ·<br/>API router · statements · presence · evidence register"]:::tool
  B[("Case board<br/>facts F# · documents D# · links L# · hypotheses H# · questions Q#<br/>recorded statements W# · claims · contradictions X# · leads K#<br/>evidence register · matrix · summary · gaps · audit")]:::board
  ING["Ingest: A1 Graph analyst · A2 Document reader ·<br/>A4 Dossier · A5 Entity resolver"]:::ing
  CDR["CDR team: R1 Intake · R2 Enricher · R3 Patterns ·<br/>R4 Location · R5 Cross-CDR · R6 Fact writer"]:::cdr
  O --> O1
  O4 --> O1
  O1 --> TB
  O1 -. "quick view" .-> S1
  O1 --> O2 --> O5 --> O
  O1 --> O3 --> B
  S4 --> Q1 --> Q2
  Q1 -- "approved" --> O1
  SX --> S1
  S1 -- "findings" --> Q3 -- "one per reply" --> O1
  S1 --> TB
  B -- "change log wakes" --> S2 --> S1 --> S4
  S1 --> B
  B --> RT["Report team: P1 editor · P2 evidence · P3 statements, timeline ·<br/>P4 analysis · P5 summary · P6 checker · P7 story"]:::board
  ING --> B
  CDR --> B
```

## The new case

The target is searched to learn who he is involved with; the case being worked is a
**new** one - the incident the officer describes (what, where, when, the FIR once there is
one). Previous FIRs, his and his associates', are background: `linkgraph/relevance.py`
says how each bears on the new case (same kind of crime, same area, people linked to the
target, recent), and the FIR answers, Sherlock and the summary all say it. "This case" in
a question means the new case once it is described.

## The new case and the past records

The case being worked is what the **officer describes** (`case.incident`) - not the FIRs
already on file. Three situations, and only the officer can say which:

1. the target has no earlier record - a suspect in a new matter;
2. the target has earlier cases - they are his history; he is a suspect in a new matter;
3. the case IS an FIR already on file in which the target is accused and was arrested -
   then that FIR, and only that FIR, describes this case.

So when an FIR is on file and the officer has not said, Sherlock's first question (red) is
**which case this is** (`case:which`). The recorder notes the answer (`this_case`,
`this_case_fir`, `target_in_custody` -> `case.dialog["case_basis"]`, `incident.fir`,
`incident.target_status`). Every agent is given `the_new_case` (`relevance.the_new_case`)
and the rule `runtime.NEW_CASE`: a past FIR's date, place, offence or people are never
given as the new case's; while the officer has not said, the target is "the target", not
"the accused", and nobody is said to be arrested. Asked about "this case" before it is
described, the reply says so, names the FIRs on file as past records, and asks.

**Telling the three situations apart.** With no record on the target, Sherlock asks what
happened, where and when. With a record, the first question names the target's recent
crimes (FIR number, police station, offence, role, the register it comes from - latest
first, `questioner.recent_crimes`) and asks: *are you investigating him for one of these
same crimes - and was he arrested in it - or is this a new matter?* "New matter" keeps the
record as history. "The same case as FIR X" makes that FIR this case's own: its offence,
date, time and place are taken from the FIR file (`recorder.adopt_fir`, marked
`incident.from_fir`) and are not asked again. One target is never asked "who is the main
suspect?" - entering him said so.

**No case at all.** The officer may only be looking at someone's record ("he is not in any
case, I am just checking his past record"). The recorder decides this first, before
anything else it writes (`purpose` is the first field of its reading - written after the
entries, the office model followed its own entries instead of the message):
`purpose: "only checking the record"` sets `case_basis = "record_check"`. Then nothing
about an incident, its roles or its FIR is asked (`questioner.NO_CASE_QUESTIONS`, closed;
`only_the_record`), what the model files with it ("not involved in any case" as an
offence, the person as a suspect, an answer to an open question) is dropped, and the
report is the target's record with "the officer said there is no case" in place of "has
not said". Describing an incident later makes it a case again, and its questions return.
Which case a message is about - no case, no event, a new one, or an FIR on file - is the
Case matcher's answer (below), which overrides the recorder's own reading.

**Plain words where the model must weigh, a form where it must fill.** Measured on the
office model (Qwen 27B) on 2026-10-21: inside a JSON form it could not weigh a message
against the records - it read "X nahi, Y!" and a bare name as news,
carried "complainant" from an earlier question to "tell me about his FIRs", called a man's
death a new matter beside the FIR of his disappearance, and listed lab findings without
weighing them. Asked to reason in plain text and end on a fixed line, it got these right
(30/30 messages, 22/22 roles). So these decisions are asked that way (`llm.generate_text`),
and code reads only the last line:

| Who | Last line | Decides |
|---|---|---|
| O0, `question_reader.act_in_words` (alongside the JSON reading) | `ANSWER: ask / point / tell / both / greeting`, `ROLE:`, `JUDGMENT:` | what the message does - "point" re-reads the officer's last question aimed at the person named (`question_text`, `case.dialog.last_question`); the role asked about; whether Sherlock's view is wanted |
| O3b Case matcher, `team/case_matcher.py` (alongside the recorder) | `ANSWER: no case / no event / new / same <FIR>` | which case the message is about - each FIR given with its first information and its reports (`its_reports`), the FIRs that name a target first |
| Sherlock's view, `quick_view.ask` | `VIEW:` | his judgment, weighed on the reports and this case's FIR |

With no plain-text model (Ollama) the JSON reading decides, as before. A role is taken only
from a message read as a question - a wrong one hides FIRs from the answer.

**Who a document is about.** `readers.link_people` ties a document to a person by CNIC,
phone, plate or name. A phone that is a target's links the target only (a SIM in a
relative's or a stranger's name is not tied by it - before, one holder was kept at random);
a name written as one word or two ("میردانش" / "میر دانش") is the same name; a laboratory or
medical report is about whom its FIR's file names by an identifier. Boards from before this
are pruned (`prune_phone_links`) and linked again once, when opened. An FIR file that names
a person by CNIC or phone is one of that person's FIRs (`case_queries._firs`, "named in the
FIR file"). O1 is given the reports on the people asked about up front (`reports_on_them`,
scanned page-header lines left out) and this case's FIR text; the Answer checker reads
those first, so a true finding is not sent back as unsupported. `network.resolve`: a match
on the distinctive words of a name must carry the officer's other words too ("Mir Danish" is
not Mir Shakeel because "Danish" is a common name), and sound keys squeeze a doubled letter. An Urdu name is also
tried with its Arabic article silent ("Abdur Rehman" is عبدالرحمن, "Shamsuddin" شمس الدین).

**O4 Case reader - the board read whole before any answer** (`team/case_reader.py`). An
answer used to start from what a search for the question's words found, so a question about
the case could be answered from an old FIR while the reports that settle it, filed under
another FIR, were never in front of the writer. Now, before every reply, code reads
the board as a whole - nothing skipped, no model waited on: the case being worked and the
FIR that IS it; every FIR of each target (the systems' list and the FIR files that name
them by CNIC or phone), each with what happened, where it stands and its medical and
laboratory reports with what was found; other documents about them. It is kept on the
board (`case.dialog.case_read`) and read again only when the board changes. O1 gets it
first (`case_read_first`), the Answer checker reads it first, Sherlock's view is given it.
When the officer moves the case to another FIR, what was taken from the earlier FIR is
dropped first (it had left the new FIR's number with the old FIR's date, place and offence). A new
link takes the next free id - one dropped link no longer lets the next overwrite another.

Each FIR in the reading carries its `first_information` in its own words beside the model's
summary - a summary can be wrong about who was last with someone, and O1 is told the record
stands. Questions of meaning or method ("what is GSR", "which reports settle murder or
suicide") are answered from investigative knowledge in a section 'General guidance (not
from the records)', which the checker does not flag; everything else stays tied to records.
The checker judges a cited document by what was read in the whole of it (summary and
findings), not by the start of its text - a lab report's start is its sample list. A
scanned report's letterhead lines are no longer facts (`report_facts` keeps the record
header only) and are never quoted as evidence. The context pack's "missing" list counts the
documents linked to the people too, so a search that missed the reports cannot make "no lab
on record" a fact. Chat time limits (`ChatSettings`): reply 120 s, draft 75 s, view 45 s,
check 20 s, revision 25 s; `SHERLOCKS_LLM_TIMEOUT` 120 s.

**P9 Network writer - the target's network, up to two steps** (`team/network_view.py`, part
`network`, section after "The target", in the brief and in the record-only report). Code
only, from the link graph: everyone tied to a target directly (how the records connect them,
stated or found by comparing records, their part in this case, criminal record), and one step
further only the people who matter - involved in this case, with a criminal record, or tied to
two or more of the direct connections - each with whom they are reached through. A police
officer, or anyone tied only as the investigating officer of an FIR, is left out unless
involved in this case. The page draws the target in the middle, the first ring, the outer ring
beside whom each is reached through (fewest crossings), involved people red, criminal records
orange, stated links solid and compared ones dashed - then a table for each ring. What is left
out is counted. Capped at 30 direct and 24 further people.

**The target's record in tables** (P8, `record_writer`, both the brief and the record-only
report). Earlier cases are split by the target's part in each FIR: *as complainant* (date
and time, police station, what the case is about in two or three lines, FIR, charges and
where it stands), *as accused* (the same, with who filed it and how the records relate that
complainant to the target), and *named otherwise* (witness, victim, named in the file).
"Where it stands" knows untraced (A-class, عدم پتہ) apart from cancelled. Hotel stays: check-in,
location, hotel and room, who stayed there at the same time, check-out, how long. Tenancy
records: landlord, tenant, witnesses, date and time (possession, registered, released, last
updated), property and other details, the target's part and the record number - matched by
CNIC, and by phone only where that party has no CNIC (one number is often on several people's
records). Reports on the target: each report tied to them, in three or four lines. What each
person states in the FIRs: per FIR, each person, their part and their words, two or three
lines. In the brief, the case's own reports and statements keep their sections and are not
repeated. A table cell with Urdu names and numbers reads left to right, so CNICs and phones
are never turned round.
A stored report carries the build of the report team that wrote it (`report_graph.REPORT_BUILD`):
one from an older build is written again, never only re-drawn with the new layout (its parts
lack what the layout reads). Raise `REPORT_BUILD` whenever what a part holds changes.

**P10 Case analyst** (`team/case_analyst.py`, after the story, before the brief). *Forensic
reading*: each report of the case and on the target is read in full (a form): laboratory,
report no., who submitted, received and issued, every exhibit with its test and RESULT, the
findings with their numbers, what they indicate (Sherlock's reading, labelled) and what the
report does not state. *Statements*: every statement of the case's FIRs and the target's
earlier FIRs explained in plain English, with the person's own words below it, never cut.
*Weighing the evidence* (plain text, labelled lines): the question the case turns on, each
explanation with what supports it, what goes against it and what would settle it (a finding
that rules an explanation out outweighs findings merely consistent with it; a record's own
words outweigh a summary of it), Sherlock's view, clashes between statements and reports,
open questions, an action plan by priority, persons of interest (never an officer doing his
duty) and up to two sections of Sherlock's own. The brief's "still missing", view and next
steps come from it - this case's, never another FIR's open points. The brief's order:
glance (with "Under inquiry"), brief, the victim or the target, the story, forensic and
medical findings, other reports and documents (uploads, read in full), weighing the
evidence, statements and what does not fit, people, police on the case and persons of
interest, the network (a number on many people's records stands once, not as a
tie; the father by the father's name on record; each person's FIRs and offences, the serious
ones marked), earlier cases, hotel stays, tenancy, leads resting on this case's records,
case papers, open questions, the action plan, Sherlock's own sections, annex.

*Since 2026-10-23*: a report filed twice (the same text under two record numbers) is read
once and says so; the laboratory system's received date stands over a reading of the scan; a
finding with several possible causes names them all; something not tested is never "absent";
residue on the hands shows nearness to a firing, not who fired; no one is called a suspect
unless a record does; a clash is two records that cannot both be right, never a guess at a
motive. A second plain-text call checks each claim of the weighing against what the reports
and documents found and takes out what misstates them (kept in `weighing.checked`). The
uploaded documents feed the weighing, the story (their dated steps) and the people (a
separate call lists the people, so a long story cannot crowd them out).
Before that check, a completeness pass adds any decisive finding the weighing left out (the
direction of the shot from the hole sizes, a board's finding, a missing weapon - never how the
investigation was handled), under the explanation it bears on; a measured finding is given
with its measurement and its own report. The check also takes out a point that does not bear
on the explanation it is under (a kidnapping FIR is no support for suicide), and shows the
checker the FIR and statements. A line goes only when the checker says the record contradicts
it or does not say it - never over wording, and never when its own reason calls the line
correct or reasonable; Sherlock's view is his labelled opinion and is not cut. A separate short
call puts each point on its side (MORE / LESS / NEITHER for that explanation: a missing weapon
supports "a third party did it"; a finding that fits an explanation is support even when it fits
another too) and takes out what does not bear on it. Last, by code, a report's own measurements
of a direction or distance (hole sizes back and front, blackening) are written into the point
that rests on that report when the analysis gave the conclusion without them.

**General for any case.** No prompt carries one case's facts or one kind of crime: the rules
are general (a finding that rules an explanation out outweighs consistent ones; a trace that
fits several explanations decides none; name every cause a finding could have; give a record's
measurements - a size, an amount, a weight, a time - with the point). The measured-findings
step matches any measured finding (mm, kg, Rs, %, hours...) to the point about the same thing,
so a transfer of Rs 4,500,000 in a fraud is given as surely as a wound's size in a death. The
case papers come from the offence itself (`offences.classify` of the FIR's sections): cheque
and fraud (the cheque and return memo, bank statements, agreements, a handwriting expert),
theft and vehicle theft (ownership papers, registration, identification parade), narcotics
(chemical report, weight and sealing memo), sexual offences (medical examination, DNA, 164
statement), extortion and threats (recordings), arms, terrorism (JIT/CTD) - and what the
records show happened (a death, a firearm, an exhumation). Examples in code comments and tests
use made-up names only.

**P11 Case papers** (`team/case_papers.py`, code only): from the kind of case (a death, a
firearm, an exhumation from its records; a kidnapping or robbery only from the FIR's sections)
each paper it needs - FIR, 161 statements, case diaries, site plan, recovery memos,
post-mortem, MLO, inquest (174), inspection memo, chemical, DNA, exhumation, FSL on casings
and weapon, arms licences, CDR/IPDR, phone extraction, CCTV with DVR seizure memos,
identification parade - is "on the board", "mentioned, not on the board" (another document
speaks of it: get a copy) or "not on the board". Its section comes before the open questions.

**P12 Group writer** (`team/group_view.py`, code only): when several targets are searched
together (a gang, a group of accused, a family) - for any kind of case - the report's third
section becomes "The targets" (one table: CNIC, father, every phone and address, criminal record,
group) and a new section "How the targets are connected" follows it:
the groups the ties make (and the targets nothing ties to the others, with what to check); each
tie between two targets - stated by a record (accused together, family, one filed against the
other), found by comparing records (the same phone, phone numbers in sequence - SIMs often bought
together - the same address or the same chak/goth/village) or through a common contact; names on
a record that may be a target (a person an FIR lists without a CNIC whose name sounds the same as
a target's - a question, never a tie that joins groups; it says which groups it would join); the
people tied to two or more targets, directly or one step on (never through a complainant or a
witness of their FIR); and each target's contacts to investigate with CNIC, phones and address.
Its lines go to the brief writer and the weighing (HOW_THE_TARGETS_CONNECT, from the records,
never the officer's words). The report writes about up to 12 targets in full
(`case_scope.MOST_TARGETS`, was 3); with several targets the network section keeps its picture
and step 2 and points to the contacts tables instead of repeating them.

**P13 Section writer** (`team/section_writer.py`): the fixed sections are in every report; P13 adds
the headings and tables THIS case needs that they do not give. After the case analyst it reads the
finished parts (targets with phones, addresses and vehicles; earlier cases; hotel stays; contacts and
common connections; reports read; uploaded documents; statements) and the list of fixed headings, and
writes labelled lines - `SECTION: heading | why`, `PLACE: targets|story|network|record|end`,
`TABLE: columns`, `ROW: values`, `POINT:` (up to four sections, six columns, 25 rows). Code keeps only
what the records hold: a row or point whose numbers (FIR, phone, CNIC, date in either order, amount)
are not in the material, or that names nothing from it, is dropped; a heading that repeats a fixed
one is dropped; ids not on the board are removed and ids written in another cell move to the source
column. The prompt names kinds of sections (offences across FIRs, vehicles and weapons, phone numbers,
recurring places, money moved, arrests by date) - never one case's facts. The PDF draws each where
P13 placed it, sized to what its columns hold, under "Added by Sherlock for this case".

**No repeats.** Before a section is added, each of its rows and points is compared with what the
report already prints, item by item: each target, contact and tie, each step of the story, each
exhibit and finding of a report (and the report as a whole), each earlier FIR, hotel stay and point
of the analysis - not the long narratives (an FIR's first information, a statement in full), so a
table that gathers facts out of those is new. A row is a repeat when one item already holds its
numbers (a date in either order) and most of its words; the same people or one FIR number found
elsewhere is not enough. Repeated rows go; a section that is mostly repeats goes whole, whatever its
heading; each added section counts as shown for the next. Headings that name the same kind of section
(plurals; chemical, DNA, lab and medical as forensic; SIM and mobile as phone) are repeats too, and the
analysis's own extra sections go when a fixed or added section already holds them. A column empty in
every row is dropped; a value written as "implied" or "probably" is no row.

**Open questions are checked.** Before the report lists a question as open, the case analyst checks it
against the whole case material: answered (moved to "Already answered by the records", with the answer
and its record), partly (the question stays, with what the records give and what is missing) or open.
An answer whose numbers or names are not in the material is not taken.

**Which case, again, and the laboratory asked again** (`team/case_rematch.py`, from the case team's agenda). The
Case matcher can only compare the officer's words with the FIRs read so far: a FIR file read a minute later (the
kidnapping FIR of a person now found dead) was never compared, and the case stayed "a new matter". So whenever a FIR
file arrives that the matcher has not seen and the case is tied to no FIR, the officer's own description is matched
again; a match ties the case to that FIR and the officer is told in the chat. A laboratory system that answered with
an error (HTTP 500, a timeout) is asked again - three tries, five minutes apart - and the officer is told when the
reports come in.

**CDRs: the archive and the CDR analyst.**

* **CDR archive** (`evidence/cdr_archive.py`): the iCOP archive of every CDR officers have already
  extracted, searched by a mobile number wherever it appears - the subscriber (A party) or a contact
  (B party). Each file found is downloaded (only from the archive's own host) and read like an upload,
  so the CDR team analyses it and its findings become facts with their rows; the document records the
  archive's details (whose CDR, A or B party, FIR, who extracted it), is filed under the person whose
  number it is, and is never read twice. **Searched automatically** (the case team's agenda, in a
  thread of its own): every number of every target, then every number of each person tied directly to
  a target - never a police officer there on duty, nor the complainant or a witness of an FIR in which the
  target is accused (the other side); in the target's own FIRs (a victim's, a complainant's) the family who filed
  or witnessed are searched. All numbers are searched first, then each CDR found is
  read; a number that joins the case later is searched on the next sweep. **Live in the chat**: one line
  for the search (how many numbers of whom, how far, whose CDR was found), one line a CDR found ("found
  the CDR of X - processing", animated, then "done" with its findings). What was searched and found is
  written on the board (`Search of the iCOP CDR archive`: one fact a CDR found, one a person with none).
  In chat the toolbox's `cdr_archive` searches any number of the case; files found are read in the
  background. Every search goes through the guard and is logged; archive searches have limits of their
  own (`archive_searches_per_case` 300, `archive_searches_per_hour` 200), apart from the NADRA, SIM and
  CRO calls. Settings: `ICOP_CDR_ARCHIVE_URL`, `ICOP_CDR_ARCHIVE_KEY` in `.env`.
* **R8 CDR analyst** (`team/cdr_analyst.py`): any question about the call records is COMPUTED, never
  read. The model is given what the records are - the CDR files on the board, the fields of a record,
  the case's numbers and the fixed analytics - never the records, and writes a few lines of Python.
  The code runs over every record of every CDR on the board in the **sandbox**
  (`evidence/cdr_sandbox.py`): parsed and checked first (only a few standard modules; no `open`,
  `exec`, `eval`, `getattr`, `__import__` or `_private` attribute), then run in its own Python process
  with no environment, a temporary folder, and limits on CPU, memory, file size and processes; a
  failed run goes back to the model once with its error. The code is kept on the board with its
  question. In chat the reader marks a question about call records (`call_records`), and the analyst
  runs before the draft (its own budget, `cdr_budget_s`); the officer agent is told the CDRs' records
  are not in what it was given and answers only from the computed result.
* **Fixed analytics** (`evidence/cdr_tools.py`, standard library only, loaded into the sandbox as it
  is): files, involving, of, top_contacts, between, common_contacts, window, on_day, daily, by_hour,
  night (across midnight), silences, devices (IMEI / IMSI changes), places, near, together (the same
  tower within minutes), first_last, before_after, km, norm - each returning the file and rows its
  results rest on.
* **Reading CDRs** (`cdr.read_events`): call type as kind (call, SMS, data) and way (in, out),
  duration in seconds, IMSI, coordinates from a combined "longitude and latitude" column or written
  after the tower's address.
* **No invented numbers**: the answer checker fails a reply that gives a phone number found in no
  record, no document and nothing the drafter was given.

Also in the brief: police on the case in their own table (who registered, investigated and
sent exhibits - ranks such as P/ASI, SIP, PI/IO are read as police everywhere, so an officer
in someone's old FIR never enters their circle); family the case's records name join the
close circle and the network even when no police system ties them; a tenant on someone's
tenancy form is never a step on in the network; an AVLC "stolen vehicle" record marks the
owner (a victim), not a criminal record; the father's tenancy shows when the target has none;
a check-out at exactly midnight is a date without a time; the cover carries no board version
and a disclaimer (made by Sherlock AI Agent, Sindh Police; may contain errors; verify every point
against its source before acting). The network picture labels its rings: step 1, direct
connections; step 2, one step further - and the section and its tables say the same.

**Hint keeper** (`team/hint_keeper.py`): after each answered question, in the background,
what the reply noticed is proposed as hints (what the records show | why it matters | next
step | ids) and kept as leads `K#` (`found_in: chat`, the turn) - only when it rests on a
record of the case (a report's or FIR's finding, a statement, a link proven by CNIC or
phone), never on the officer's words, a graph pattern or a common pattern, and never twice.

## What each police system tells

`linkgraph/systems.MEANING` says, for every system, what its record is and how a link from
it reads (taken from the shape of each API's answer, which `extractors.py` parses): a
tenancy witness vouched for the tenant and is not a co-tenant; a registered owner of a SIM
is who the number is registered to, not always who uses it; "shared hotel stay" means the
same stay, "used the same phone at check-in" does not. The Officer agent gets the
explanation of every system in play (`what_each_system_tells`), and the **dossier** of a
person carries what each system returned about them: `records`, `hotel_stays`, `vehicles`,
`organisations`, `police_officer`, the systems searched with nothing found and the ones that
could not be checked (an error is never reported as "no record").

**The tenant register (TRUST)** answers with the person's tenancies by the part they have
in each: `tenacies_as_tenant` (with the landlord, `witness1`, `witness2` and
`family_members`), `tenacies_as_witness`, `tenacies_as_family_member` and
`properties_owned`. `extractors._trust_tenancies` reads each list for what it means, so a
link is "Tenancy witness", "Landlord", "Tenant (home the subject lives in)", "Family
member, same rented home" - never "named in property record" - and carries the property,
the landlord, the lease dates, the status and the police station. The `relation` field
beside a witness is his father's name; it is read as a relation only when it is a kinship
word. `relations.py` turns each label into its sentence ("A was witness to the tenancy of
B"). A link's label is fixed when the graph is built: older runs need a new search.

**Who a name means** (`network.resolve`): every name the records give a person counts, not
only the one shown - the exact name first, then a word of it, then the same name in the
other script, and a look-alike spelling only last ("hassam" is not "Hassan" when someone
on the graph is called Hassam).

## The graph: whose name, which address

Three rules of the link graph (`linkgraph/graph.py`, `linkgraph/weak_links.py`):

- **Whose name is shown.** A person's name (and father's name) comes from our own
  registers first: NADRA, then the driving licence (DLS), then the tenant registers (PRVS,
  Old Tenant, TRUST), then the other police systems. The telecom **subscriber API** comes
  after all of them, name tags and the open web last (`graph.NAME_PRIORITY`). Other
  spellings are kept as "also recorded as".
- **The same address means the same place.** Two addresses match only on the same
  premises (house / flat / plot, street, block) or at least the same named locality
  (mohalla, colony, village - a weaker "same locality"). The same city, district or
  province alone is never the same address, and the model saying so is not believed
  (`weak_links.address_score`).
- **Co-witnesses are co-witnesses.** People who witnessed the same tenancy, verification
  or case are linked as "both witnesses in ..."; the address written on that form does not
  make them "the same address".

## The case board

`evidence/case_file.py`. Every entry carries where it came from:

| Field | Meaning |
|---|---|
| `agent`, `event` | who wrote it, and the batch it belongs to (one upload, one CDR, one correction) |
| `tier` | `fact`, `inference` or `speculation` - how sure the claim is |
| `trust` | 1 system record · 2 document · 3 the officer · 4 derived by us · 5 unverified - who it comes from (`evidence/trust.py`) |
| `rests_on` | ids and topics it was built from (`F12`, `incident:date`, `owner:D9`) - what a correction follows |
| `replaces` / `replaced_by` | a correction: the old entry is kept, never deleted |
| `stale` | rests on something that changed: out of answers until the agent that made it redoes it |
| `rows`, `page` | where in the file or document it is |

Rules: **add, never rewrite**; every change goes into the change log (`case.changes`)
with its agent and event; listeners are told once per batch, never for a batch still open.

Besides facts, documents and links the board holds: the incident, roles, the question
ledger, the conversation, hypotheses `H#` with their history, the summary and timeline,
ranked gaps, dossiers, Sherlock's quick views given in chat, the Question desk's decisions,
follow-up messages, the audit log of live calls and the agenda of background work.

**Where it lives** (`evidence/board_store.py`): its own table `case_board` (migration
`9c4e1a7b2d30`), keyed by run, with the board's version. A save older than the stored board
is refused; a board posted back by the host that is older than the stored one does not
replace it - only the officer's own edits in it (roles, incident) are merged as new entries.
Every caller gets the **same** board object for a run (`RunManager.board`), also after the
run has left memory. A copy stays in the run's graph (`graph.case`) for exports.

## The Officer team: one reply

`team/officer_graph.py` (entry point `linkgraph/sherlock_team.run_turn`, which streams the
graph's events to the chat):

```mermaid
flowchart LR
  read["read · O0 Question reader"] -->|no model| rules["rules (fallback)"] --> done([END])
  read -->|tells us something| record["record · O3"] --> desk
  read -->|asks| brief["brief · A4, O4, Facts agent,<br/>the shape's sources"] --> draft["draft · O1"]
  draft -->|lookups named| lookup["lookup · toolbox / API agent"] --> draft
  draft --> desk["desk · Sherlock's view, Q3, Q1"] --> check["check · O2"] --> present["present · O5"] --> done
```

1. **read - O0 Question reader** (`linkgraph/question_reader.py`). One model call reads the
   message: the act (question, statement, answer, correction, greeting), what is wanted, the
   people, the role asked about, the scope, the **answer shape** (FIR, person, incident,
   statement, link, phone, count, yes / no, what next), the depth (full unless the officer
   asks for short), whether Sherlock's judgment helps, and the language. **Its reading is
   the ask.** A message that asks anything is a question, even if it names someone Sherlock
   asked about. No model, or no answer from it: the turn goes to `rules`.
2. **record - O3 Statement recorder** (`team/recorder.py`), when the officer tells us
   something. The model breaks the message into entries, one fact each: who told us, the
   **original speaker** ("muddai ne bataya ke..." is two people - the entry gets the
   complainant's trust, not the officer's), about whom, what, where, **when it happened**
   ("kal raat 10 baje ke qareeb" becomes a date and time from the message time, with a
   30-minute tolerance, the officer's words kept), **how the officer knows** (saw it, a
   record, heard it, an informant, thinks so - which sets fact, inference or speculation)
   and how sure. It also says whether an entry **corrects** something on file (then
   `linkgraph/corrections.py` replaces it and marks what rests on it stale), which open
   question the message **answers** (by what the answer is; "don't know" closes it too), a
   yes / no to something Sherlock asked to confirm, and what the officer wants of the desks
   (stop asking; hold Sherlock's findings). O1 reads back in one line what was recorded.
3. **brief** - for a question: **A4 Dossier agent** (the card of each person asked about),
   the case board and graph (the knowledge base), **O4 Case briefer** (`linkgraph/briefer.py`:
   the context pack at one board version), the **Facts agent** (`linkgraph/case_queries.py`:
   exact counts and lists, computed - never left to the model to count), and every source
   the answer shape needs (`team/shapes.SOURCES`: statements, the presence matrix, the
   evidence register, contradictions, leads...). Sherlock's quick view starts alongside
   when the reading says his judgment helps (a person's involvement, why, what next) - not
   for counts, lists and lookups.
4. **draft - O1 Officer agent** (`team/officer_agent.py`). Given all of that plus the open
   contradictions and leads about the people asked, he writes the answer into the shape's
   sections, each line with its source id. **No list of question types limits what he may
   answer.** When what he was given does not hold the answer he names the **lookups** that
   would get it: any tool of the shared toolbox, with its arguments.
5. **lookup** - the graph runs those tools in parallel, inside the tools budget. The live
   ones (`api_router`, `lookup`, `fetch_fir`, `fetch_lab_reports`, `fetch_cro`,
   `cdr_lookup`) are the **API agent**: police systems and FIR files called at run time,
   through the guard, logged under the officer. What comes back goes on the board and O1
   drafts once more (two drafts at most - never a loop).
6. **desk** - nothing found anywhere: the reply says so plainly, "I did not find an answer",
   with every place checked - never a guess. A question that needs real work (O1 says
   `deeper`) is **not** investigated inside the reply: it goes on Sherlock's agenda, the
   reply says he is looking into it, and what he finds comes back through the Insight desk.
   (planning it wakes his run; his conclusion is told as a finding). Then Sherlock's view is collected, and **one extra item** is chosen: a critical finding
   from the **Insight desk** first; else the Gatekeeper's question; else another finding.
7. **check - O2 Answer checker** (the finding is checked like the answer: one that cites an
   entry not on the board is held back; a reply that still fails after one revision goes
   out with a visible "not verified" line), then **present - O5 Presenter**: the answer first, the
   shape under headings, evidence quoted, Sherlock's view in its own block, the finding
   ("Sherlock ki nayi baat (tasdeeq nahi)") after the answer, the question last.

### What the model is held to

Found by running the office model (Qwen 27B, about 60 tokens a second) on a live case:

- **Every field is asked for** (`team/runtime.Typed`). With guided decoding the model leaves
  out any field that has a default: no shape, no depth, no sections came back. Every schema
  the model fills marks its fields required in the JSON schema (a list of many small details
  asks only for the ones that matter: `runtime.asking`).
- **Code settles what the model is loose about** (`officer_agent.normalise`): a section
  with points is *filled* whatever status it was given; a source in brackets stays only
  when it is a real entry of the board or a police system whose record was given
  (`runtime.real_refs`) - an invented id or the name of a field is taken out.
- **The chat is context, never evidence.** O1 is given the conversation only to follow it;
  O2 judges a draft against the records it was written from, the text of the documents it
  cites and the officer's own words - never against Sherlock's earlier replies (which once
  "corrected" right drafts into wrong ones).
- **Plain words** (`runtime.PLAIN`, in every agent that writes for the officer): what the
  records show, never network-analysis terms (broker, gatekeeper, cluster, centrality). A
  SIM registered in another person's name is common: a fact on the board, never a lead.
  Call records never show what was said.
- **Background calls are sized to finish** inside the model's 60-second call limit: small
  batches, short outputs, at most 18 fields a record card.

### Answer shapes

`team/shapes.py`. For each kind of question, a fixed list of sections the answer must
cover - for an FIR: what happened, when, where, who, why (as stated, and by whom), weapon
and property, statements, evidence, status, contradictions and leads, not on record. Full
by default; short only when the officer asks. O1 fills each section with sources or marks
it **not on record** / **not checked yet**; a section he drops is put back, marked, and
O2 records it (`checks.rules.complete`, `checks.restored_sections`). The shapes are data:
nothing in them reads the officer's words.

**Time budgets** (`linkgraph/budget.py`, settings `chat.*`): each step has a limit and a
fallback; the whole reply never takes over 40 s. What finishes late is sent as a
**follow-up** message in the same chat (`case.followups`; the portal polls for them).

| Step | Model | Budget | Over budget |
|---|---|---|---|
| O0 Question reader | one call | - | no reading: the rules turn |
| O3 Statement recorder | one call (when the officer tells us something) | 10 s | the rules turn |
| O4 Case briefer, the shape's sources | no | - | - |
| O1 draft | one call; a second after lookups bring something | 18 s (a full shaped answer is 400-600 tokens, about 10-15 s on the office model) | a first draft that answered goes out; else the rules answer from the board now and the model's answer follows |
| Lookups / live calls | no | 10 s in all, in parallel | answer without them; what they bring is on the board for the next question |
| Sherlock's quick view | one call, alongside | 10 s | "Sherlock is looking into it"; his view follows |
| Gatekeeper's reading of the chat | one call, alongside | 4 s at the desk | nothing new is asked this turn |
| O2 model check | one call | 6 s | rules only this time |
| O1 redraft | only when the check fails: the draft goes back to O1 once, with the reasons | what is left, up to 18 s | the first draft goes out with a "not verified" line |
| O5 Presenter | no | - | - |
| Deep investigation | Sherlock, in the background | never inside the reply | his conclusion comes through the Insight desk |

Every turn records each step's time, its model calls and what ran late (`final.timing`,
`final.model_calls`, `final.late`).

**While the officer waits** the chat shows one line saying what runs now - "Searching the
case board", "Checking NADRA · Kamran Ahmed", "Sherlock is thinking it through" - in the
officer's language, as shimmering text (`conversation.STATUS`, `TOOL_STATUS`; `status`
events on `POST /graph/investigate`). Fixed text, never written by the model.

### O2 Answer checker

`sherlock_team.validate`, code first:

| Rule | Check |
|---|---|
| Answered what was asked | a count question opens with the count; the reply does not open with a question |
| Correct | numbers match the Facts agent's result |
| Sourced | every id exists and is not replaced or stale; quoted text must be in the document it cites - a quote that is not is removed |
| Consistent | nothing contradicts the officer's statements (the model is asked only for claims no query computed) |
| Honest about gaps | nothing found, and the reply says so |
| Labelled | speculation is marked as Sherlock's view |

Sources that disagree are **shown, never hidden**: the reply leads with the more trusted one
and names both (`trust.conflicts`). Exception: for the incident's time and place and for
roles, the officer's word leads over the FIR.

### O5 Presenter

`linkgraph/presenter.py`: the first line answers, in bold; three or more FIRs become a
table (only when it keeps every number the bullets had); up to two short quotes from the
records with their source (translated when the officer's language differs and a model is
connected); Sherlock's view in its own labelled block; the question last. A **format guard**
compares every number, CNIC, phone, FIR number and source id with the checked answer; if
anything changed, the checked answer goes out as it is.

### The Facts agent and the knowledge base

Exact answers by code (`linkgraph/case_queries.py`, `offences.py`, `narrate.py`):

| Query type | Example | Answer |
|---|---|---|
| `cases` / `count_cases` | "ye kitni FIRs mein?" | FIRs with number, police station, role, crime in words, source; the count |
| `fir_status` | "kis FIR mein acquitted, kis mein convicted?" | each FIR's outcome from CRO / PSRMS and the FIR file |
| `serious_cases` | "koi khatarnak FIR?" | heinous / serious crimes first; FIRs where he is the complainant said apart |
| `fir_details` | "FIR 391/26 ki tafseel" | crimes, role, complainant, date, accused, the complainant's account, status |
| `io_report` | "IO ne guilty declare kiya?" | what the investigating officer concluded (challan, A/B/C class, 169), quoted |
| `criminals_near` / `criminals_all` | "his connections mein kitne criminals?" | people with a record, with how they are linked |
| `profile`, `connection`, `associates`, `hotels`, `phones`, `vehicles`, `documents`, `graph_stats`, `news`, `summary` | | from the graph and the board, each item with its source |

Every other question is answered from the live **knowledge base** (`linkgraph/knowledge.py`):
all people, record fields, links, FIR files and diaries, documents, the officer's
statements and Sherlock's hypotheses (corrected and stale entries left out), searched by
words, concepts in three languages and sound. A question about a person returns only
entries about that person; a topic no record holds is said to be missing, never guessed.

## The Question desk

`evidence/question_desk.py`. The Questioner (`evidence/questioner.py`) and Sherlock propose;
the **Gatekeeper** decides; the **ledger** (`case.questions`) keeps every question with its
topic key, how often and in which turns it was asked, its status (`open`, `answered`,
`dont_know`, `muted`) and the answer with its turn (earlier answers kept on a correction).

| Rule | |
|---|---|
| Same topic, same question | topic keys (`incident:when`, `roles:main`, `cdr_owner:D3`); free-form questions get one from their words |
| Answered anywhere closes it | the ledger, the board, or anywhere in the chat - said in passing, the answer is taken from that turn |
| "Don't know" counts | never asked again |
| Never what the records answer | "who is the victim" is not asked when the incident's FIR file names the complainant, and never offers a target or a suspect as the answer; "what ties A and B" only for two targets the records do not tie |
| A group is a group | when the officer says the targets act together ("they are one gang", "all are suspects" - a plain yes/no the recorder asks the model, `recorder.targets_together`), every target is recorded as a suspect and neither "who is the main suspect" nor "what ties them" is asked |
| Muted | "ye mat poocho" / "don't ask that" |
| Max two asks | never in two replies in a row; one per reply; none while the officer is busy asking something else |
| Checked twice | when proposed, and again just before it is asked |
| Tells Sherlock why | every rejection is logged with the reason and the answer on file; Sherlock reads them |

| Key | Asks |
|---|---|
| `incident:place` / `incident:pin` | where it happened (map) |
| `incident:what`, `incident:when`, `incident:fir`, `incident:vehicle` | which crime, when, the FIR, a vehicle or weapon |
| `roles:main`, `roles:victim` | the main suspect, the victim |
| `cdr_owner:D#`, `cdr_of:<person>` | whose a CDR is; upload the main suspect's CDR |
| `confirm_lookup:<number>` | a number that appears only in a document: may it be looked up? |
| `confirm_lookup:cdr:D#` | may the owners of a CDR's top contacts be looked up? |
| `link:why`, `suspects:other` | what ties the targets; anyone else suspected |

## Corrections

`linkgraph/corrections.py`: incident date, time and place; a person's role (also moved from
one person to another: "Kamran nahi, Imran"); the owner of a CDR. One event: the new
statement `replaces` the old, the ledger answer changes, everything that rests on the old
statement or the topic is marked stale, and the redo goes on the agenda (CDR incident window
and towers near the pin, re-filing a CDR's findings, dossiers, summary, Sherlock). The
officer is told what changed and what is being redone.

## The shared toolbox and the API router

The investigator's tools (`linkgraph/investigator.py`), the same for both teams, each team
with its own budget: **board** reader, **dossier**, **chat memory**, report reader
(`read_document`, `search_evidence`), **CDR query**, **API router**, **statements** (every
recorded statement and claim by or about a person, with what changed), **presence** (the
matrix), **evidence_register** (by person, topic, record or exhibit, with custody and
pending reports), **contradictions**, **case_leads**, **hypotheses** (the matrix and the
Skeptic's challenges), plus the graph tools. O1 picks from them by name.

The **API router** (`linkgraph/api_router.py`) maps an ask to a topic and the catalog's
systems: identity (NADRA, CFMS), work (EVS, HOPE, HRMIS, SBVS, PRVS), vehicle (Excise, AVLC,
TRACS, DLS), phone (SIMs, Subscriber, Caller ID - unverified), address and family (PRVS, Old
Tenant, TRUST, DLS), record (PSRMS, CRO, Watchlist, ARMS), complaint (PFC, IGP CMS, MILAP),
hotel (Hotel Eye), footprint (OSINT - unverified). A CNIC-only system waits for the SIMs
lookup that finds the CNIC; systems already searched for the person are skipped, including
those that found nothing; a topic no system holds is said so.

## Ingest team and CDR team

* **A1 Graph analyst** (`evidence/graph_analyst.py`) - the scenario rules' patterns (repeat
  co-offenders, shared phones and vehicles, landlords, hotel overlaps, brokers...) onto the
  board as tiered facts, while the graph builds and when it is done.
* **A2 Document reader** (`evidence/collector.py`, `readers.py`, `uploads.py`) - FIR files,
  lab reports, CRO dossiers and uploads, as quoted facts.
* **R7 Document reader** (`team/document_reader.py`) - every document the officer uploads
  (the 📎 in the chat: an inquiry or commission report, a post-mortem, a statement, a memo) is
  read whole, however long: an uploaded PDF to its last page (no 30-page cap) and kept whole
  on the board (400,000 characters, not 60,000). The text goes in parts of whole pages (~7,000
  characters), four side by side, each answering in labelled lines - EVENT (a dated step),
  FINDING (the document's own conclusion), PERSON, EVIDENCE (an item and what happened to
  it), LEAD (a line of inquiry of THIS case still to trace or check - not its terms of
  reference, reform or disciplinary recommendations) and SAID - each with words copied from
  that part; a line whose words are not in the text is dropped. Lines become facts, leads go
  on the board as `K#` (`found_in: document`, near-duplicates once), and a last call says what
  the document is, its summary and its most important points (`doc["reading"]`). The officer
  sees "📖 Reading… 3/14 parts" on the document while it is read (`reading_status`) and a
  chat message when it is done. The chat's case reader gets its steps, findings, leads and
  people with their ids; the report gets its own section, its steps in the story and its
  findings in the weighing. A 62-page inquiry report reads in about two minutes. The same
  file uploaded again is kept once - unless an older reader read it (cut at 30 pages, ten
  facts): then it is read again, whole, into the same document. Until then the chat's case
  reader still gives such an upload's summary and facts, marked "read in part". What a
  document recommends investigating reaches the chat as `recommends_investigating`.
  Every attached file has a status the chat follows (`case.upload_status`, in the case index
  as `uploads`): received, extracting (the text taken out), analysing (a CDR), reading (part x
  of y, with a bar), then done (what was found - steps, findings, leads, people) or failed
  (why). The chat shows one line a file, updated in place; the case board shows the files
  being read, a badge on each document, and a "Leads to check" zone - the chat's notes, the
  documents' leads and the rest, open ones only.
  When a document is read, the open leads it answers ("who found the body is not recorded")
  are marked `answered` with the fact that answers them (a lead status after confirmed); a lead
  noted from the chat or raised by a document keeps its own words (the lead spotter only ties
  it to a hypothesis). A family tie is written only when the record says whose relative the
  person is ("two brothers, A and B" are brothers to each other). A name word the model writes
  that no record of the case holds is put back as the records spell it, when one record name
  sounds the same (`story_writer.respelt`). Read documents never enter the lab-report section.
  In the chat, each place and time belongs to its own event: an FIR's place is where the
  reported incident happened, never where a body was later found unless a record says so.
* **A4 Dossier agent** (`evidence/dossiers.py`) - one card per person, rebuilt only when
  something about that person changed.
* **CDR team** (`evidence/cdr.py`, `evidence/cdr_team.py`): R1 Intake (columns, cleaning,
  each record's sheet row), R2 Enricher (owners of top contacts once the officer agrees, the
  handset behind an IMEI, the nearest police station), R3 Patterns (top contacts, silences,
  IMEI changes, habits), R4 Location (towers near the pin - "connected to a tower X km from the
  pin", never "was at" - movement on the incident day, likely home and work areas), R5
  Cross-CDR (calls between subjects, common contacts, the same tower within minutes), R6 Fact
  writer (each finding a fact with sheet and rows, fact or inference, filed under the number
  until the owner is known). Known formats also go to the CDR server; its report is compared
  with the team's own analysis.

## The Sherlock team in the background

`team/sherlock_graph.py` is one run; `linkgraph/casework.py` decides when to run it.

```mermaid
flowchart LR
  ingest["A2: record cards, W#"] --> entities["A5"] --> register["S10"] --> claims["S5"] --> presence["Presence matrix"]
  presence --> S6["S6 Contradictions"] & S7["S7 Statements"] & S8["S8 Leads"]
  S6 & S7 & S8 --> S2["S2 Summary"] --> S1["S1 Sherlock: hypotheses, matrix"] --> S9["S9 Skeptic, S1 replies"]
  S9 --> S4["S4 Questioner → Q1"] --> Q3["Q3 Insight desk"]
```

The order is fixed in code; no model decides who runs next and a run never loops. Code finds
the candidates, the model judges only what code cannot, and every finding cites the entries
it rests on. Without a model nothing is guessed: the work waits for the next run.

| Agent | What it does | Code / model |
|---|---|---|
| **A2** record cards (`team/register.py`), recorded statements (`team/statements.py`) | A typed card per record (FIR, MLO, postmortem, DNA, chemical, forensic, recovery memo, site plan, CRO), every field with page and quote. Statements **W#** - the FIR account, 161, 164, supplementary, interrogation - copied word for word, with speaker, role, date, who recorded it. | The model reads; code drops any field or statement whose words are not in the record (`quote_in`). |
| **A5 Entity resolver** (`team/entities.py`) | Who is who: CNIC, then phone, then name with father's name - or a person the record itself identifies. Three states: *same*, *maybe* (name only), *different*. A maybe is never merged and never makes a contradiction; one that matters becomes a question. The officer's answer decides it, and undoing a match follows through: the claims are re-read and a contradiction that only existed because two people were read as one is closed. | Code. |
| **S10 Fact collector** (`team/register.py`) | The evidence register: the cards by record, person, topic and time; **police system records** as cards too (NADRA, SIMs, Excise, DLS, Hotel Eye, tenant registration, PSRMS... each with its call id and the day it was checked; "checked, nothing found" kept); exhibits linked across records; what a record of its kind lacks ("not in report"); chain of custody with the days between steps (a long gap is flagged as a fact); reports asked for and not received. | Code. |
| **S5 Claim extractor** (`team/claims.py`) | Everything becomes a claim: said by, about, topic (presence, time, place, weapon, injury, vehicle, phone, money, relation, role, identity), value, when, where, the exact quote, trust. | Card fields in code; statements and loose facts by the model, quote checked. |
| **Presence matrix** (`team/presence.py`) | For each person, everything that says where they were in the incident window - CDR, hotel, statements, vehicles, records. Each cell says which kind of evidence it is: a phone or a hotel record is not the person. | Code. |
| **S6 Contradiction finder** (`team/contradictions.py`) | Pairs of claims about the same person and topic from different sources (and the weapon one names against the injury another found). Times inside the tolerance (30 min, or the claim's own) are not a contradiction; clearly different times for the same event are, in code. **Impossible travel** is code too: the CDR team measures distance against time between two tower contacts (over 15 km, well beyond a tower's coverage, at over 150 km/h) and S6 files it. A system's address, vehicle or phone record is compared with what the records and statements say, never with another system. **X#**: both sides quoted, severity (critical: presence, weapon, injury, identity; major: timeline, role; minor), status (open, explained, resolved), what would settle it. | Code flags; the model judges only the flagged pairs, each once. |
| **S7 Statement analyst** (`team/statement_analyst.py`) | Each person's versions in date order, what changed, where an account does not fit the other evidence, relations to the parties. Says *changed*, never *lied*. | The model compares; code keeps only findings that point at real entries. |
| **S8 Lead spotter** (`team/leads.py`) | Hints code found (a silence around the incident, an IMEI change, contact with one number jumping before it, cross-CDR patterns, graph inferences, a custody gap) become leads **K#**: why it could matter, the hypothesis it bears on, the next step, status *spotted → open → pursuing → confirmed / dead end*. A lead is never evidence. | Code finds; the model writes the one line and the next step. |
| **S1 Sherlock** (`linkgraph/casework.assess`, `team/matrix.py`) | Hypotheses **H#**, then the **hypothesis matrix**: each piece of evidence against each hypothesis, with a reason. Ranked by the evidence *against* each one. | The model sets the cells; counts and ranking are code. Strong marks only from trust 1-2 sources; a past record counts for nothing; leads never go in; one piece of evidence never makes "supported". |
| **S9 Skeptic** (`team/matrix.py`) | Tries to break the leading hypothesis and keeps an alternative open. S1 answers each challenge once (accept, or rebut with an id) - S1 → S9 → S1, never a loop. A rebuttal with no id is unanswered and shows in the report. Protected traits and past records are barred as evidence whatever S1 replies. | The model; the bars are code. |
| **S4 Questioner** (`team/questions.py`) | Questions from contradictions, leads, statement changes, identity checks and pending reports, each carrying its source id (`source:X3`), ranked by impact, worded neutrally in the officer's language; a *task* (a record to fetch) is asked once, then tracked. Each passes the Gatekeeper. The answer goes back to the entry: X# explained or resolved, K# confirmed or dead end, the two people merged or kept apart. | The model words and reads the answers; ranking and the ledger are code. |
| **Q3 Insight desk** (`team/insight_desk.py`) | Which findings the officer hears: critical and major only, one per reply, after the answer, told once (a finding that changes may be told once more). A critical one waiting shows as a badge on the case panel. The officer can hold them. | Code. |

Each run is recorded on the board (`case.dialog["team_runs"]`: model, prompt version, board
version, each agent's result and time), so it can be audited and run again.

What wakes the team: a batch from outside it finished (an upload, a CDR, a document, the
graph phase, a correction); the officer gave information; 15 facts outside any batch. It is
never woken by its own writes; a wake with nothing new from outside is skipped; several
events within two seconds start one run and runs never overlap. Background work waiting on
the board's agenda survives a restart. Runs per case: `evidence.sherlock_runs_per_case`.

### Which source wins

`evidence/trust.py`: 1 an official system record, 2 an official document, 3 the officer's
statement, 4 a recorded statement (a reliable record that the person *said* it - not that
it is true), 5 derived by us, 6 unverified.

### Golden cases

`team/golden.py`, `scripts/golden_cases.py`, cases in `tests/fixtures/golden/`. A golden
case is a closed or made-up case with planted contradictions and leads, a scripted officer
chat and an answer key. The run scores: planted contradictions found (bar 90%), false
contradictions (at most 1), the lead that mattered in the top 3, the right hypothesis
first (tracked), wrong merges (0), speculation shown as fact (0). Run it on every change
to prompts, models, rules or tolerances (`--save` / `--against` compare two runs).

## The case report: the Report team

`team/report_graph.py` (data), `evidence/case_report.py` (the board laid out),
`evidence/report_pdf.py` (the PDF). Writing the report is a different job from solving the
case, so it has its own team, a third LangGraph graph. It reads the board and writes; it
**never judges and never looks anything up**.

```mermaid
flowchart LR
  freeze["P1 freeze one board version"] --> scope["P1 what belongs to this case"]
  scope --> P2["P2 Evidence: parts A, B"] & P3["P3 Statements, timeline: C, E"] & P4["P4 Analysis: D"]
  P2 & P3 & P4 --> editor["P1 part F, annex, linked ids"] --> target["P2 Target writer"] --> P8["P8 Record writer"] --> P7["P7 Story writer"]
  P7 --> P5["P5 Summary, written last"] --> P6["P6 Report checker"]
  P6 -->|failed once| P5
  P6 --> build["P1 build: internal and file"]
```

| Part | Holds | Written by |
|---|---|---|
| Sherlock's brief | The case in short · what the evidence shows · what does not fit · what is still missing · Sherlock's view · next steps. Every sentence cites its ids. | **P5**, last, from the finished parts of THIS case (the model; templates without it) |
| Scope | What belongs to the case: this FIR, the FIRs that name it, their reports, the officer's uploads, the people they name (`case_scope.build`) | P1 |
| Target | The target's profile, part and status in the case, own earlier FIRs, close circle | P2 Target writer |
| Record | The target's earlier FIRs (date and time, police station, offence, their part, where each stands - convicted, acquitted, under trial - what the complainant said, what was found against them), everyone the records tie to them and how, their hotel stays, the earlier reports of those FIRs (`parts.record`) | **P8 Record writer** (the model reads each FIR file, one call per FIR; records without it) |
| Story | The case in date order with sources, and its people with their part and status; the case's own statements, contradictions, reports and leads (`in_case`); the case at a glance | **P7 Story writer** (the model; templates without it) |
| A · Overview | The incident as recorded, the people on the case in one table | P2 |
| B · Evidence | B1 the FIR's card and every other FIR of the people on the case · B2 medical · B3 forensic and lab, chain of custody, pending reports · B4 recoveries and site plan · B5 police system records per person, "nothing found" kept, hotel stays checked against the incident and each FIR date · B6 CDR analysis and presence · B7 the register by topic | P2 |
| C · Statements | Each person's statements in date order, what changed, where they disagree with the evidence; the officer's statements; every question with its answer | P3 |
| D · Analysis | By person; the contradictions register; the leads register; the hypothesis matrix; the Skeptic's challenges; suspicions; how the assessment changed | P4 |
| E · Timeline | One timeline from every source, with conflicting times side by side | P3 |
| F · Open points | What is not clear, why it matters, the question that would clear it and who can answer; next steps; open tasks; checked, nothing found | P1 |
| Annex | Ids linked both ways (X1 to D10 and back), the index by person, the audit of live calls | P1 |

**P6 Report checker**: every id real and current, every part present, and every summary
sentence backed by the body - a sentence that cites no id, an unknown id, or an id the
body does not use fails. A failed summary goes back to P5 once; what fails again is left
out and the report says so (`report.checked`, shown on the summary page).

All parts use the same frozen board version. The team **prebuilds** the report at the end
of every Sherlock run (`casework.work`), so a download is usually instant; its writes never
wake the Sherlock team. On download, if the board moved on, Sherlock first brings his
assessment up to date (`evidence.report_wait_s`, 90 s); past it the report is built from
his last assessment and the cover says "as of", listing what is not yet assessed.

**Two versions from the same board version.** The *internal* report holds everything. The
*file* report (`report.pdf?version=file`, `case_report.file_version`), for court and the
case file, leaves out suspicions, the hypothesis matrix, views on persons and leads; its
summary gives the lines of inquiry in place of Sherlock's ranking and view, and
contradictions stay as conflicts between sources.

Without the model every part is built from the board with templates and the report says so.

## Safety and audit

`evidence/guard.py`, for every live call:

- **Document text is data** - every prompt that carries document text says so; the model only
  proposes calls, the guard decides.
- **Lookups only for people on the case** - a CNIC or number on the graph, written by the
  officer, or confirmed by the officer. One that appears only in a document waits for the
  officer (`confirm_lookup` question).
- **The officer's access applies** - a token may carry `sys`, the systems its officer may
  use (see `docs/INTEGRATION.md`).
- **Limits per case** - `evidence.case_live_calls` in all, `case_live_calls_per_hour`; admins
  are warned near the hourly limit.
- **Every call logged** - officer, case, agent, reason, system, identifier, made or refused:
  `case.calls`, the `sherlocks.audit` log, `GET /graph/runs/{id}/audit`.

## The chat test set

`linkgraph/eval_chats.py` (`python scripts/eval_chats.py`): fixed chats in English, Roman
Urdu and Urdu, with a correction, scored on answer-first, real sources, no repeated
questions, corrections kept, and reply time. `tests/test_eval_chats.py` holds the thresholds.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `chat.reply_budget_s` | 40 | the whole reply |
| `chat.draft_budget_s`, `tools_budget_s`, `view_budget_s`, `investigate_budget_s`, `check_budget_s`, `revise_budget_s` | 18, 10, 10, 25, 6, 6 | per step (capped by what is left of the reply budget) |
| `chat.pack_chars`, `chat.recent_turns` | 14000, 4 | the context pack |
| `evidence.chat_live_calls` | 6 | live calls per chat question |
| `evidence.background_live_calls` | 4 | live calls per Sherlock team run |
| `evidence.case_live_calls`, `case_live_calls_per_hour` | 120, 40 | per case |
| `evidence.sherlock_runs_per_case` | 20 | background runs per case |
| `evidence.report_wait_s` | 90 | Sherlock's catch-up before a report |
| `evidence.cdr_radius_km` | 2 | "near the incident" |
| `EMS_CDR_API_URL`, `EMS_CDR_API_KEY` | - | the CDR server |

## API

| | |
|---|---|
| `POST /graph/investigate` | one chat turn (SSE): `start`, `status` (the waiting line), `agent`, `step`, `final {answer, checks, questions, citations, timing, late, followup_pending, shape, sections, lookups, finding}` |
| `GET /graph/runs/{id}/case` | the board now, with `hypotheses`, `summary`, `gaps`, `followups`, `agenda`, `statements` (W#), `contradictions` (X#), `leads` (K#), `matrix`, `challenges`, `presence`, `critical_waiting` |
| `GET /graph/runs/{id}/audit` | every live call for the case, with totals |
| `GET /graph/runs/{id}/report.pdf` | the case report (Sherlock's assessment, brought up to date); `?version=file` for the court / case-file version |
| `POST /graph/runs/{id}/uploads` | multipart `file` (+ `note`, `owner`) |
| `POST /graph/runs/{id}/incident` | `{lat, lon, place, date, time, fir}` |
| `POST /graph/runs/{id}/questions/{Q#}` | `{answer, pid?}` |

## The trace of each question

Every chat turn leaves a trace on the case board (`team/trace.py`, the last 30 turns):

* `steps` - each node of the Officer graph in the order it ran: the agent, the time it
  took, what it did (the reading, the draft's sections and the lookups it asked for, what
  each lookup found, the checker's verdict), and the notes the agents wrote while working;
* `model_calls` - every call to the model: which agent, in which step, what it was given
  (the parts of its input and their size), what it answered, how long, cached or failed;
* `flow` - who handed what to whom ("O1 Officer agent -> Toolbox: asks for
  api_router(...)", "Toolbox -> O1 Officer agent: api_router: found");
* `late` - what finished after the reply went out (a slow draft, Sherlock's view).

Read it over the API (same sign-in as the case):

    GET /graph/runs/{run_id}/trace            every question in brief + the background runs
    GET /graph/runs/{run_id}/trace/{turn}     one question in full (`latest` = the last one)

`background_runs` are the Sherlock team's runs between questions, agent by agent, with what
each did and how long it took. One line per turn also goes to the server log
(`Turn 5 trace: read(3048ms) -> brief(...) -> draft(...) | 40074 ms | 5 model call(s)`).

## The report: one fixed structure

The report is a **brief for the investigating officer, about the case and the target only**
(`report_graph.STRUCTURE`), in the order it is read - and the member of the Report team that
writes each section.

| # | Section | Written by | Holds |
|---|---|---|---|
| 1 | The case at a glance | P1 Report editor | FIR, police station, offence, when it happened and when the FIR was registered (with the delay), place, complainant, victim, accused and their status, investigating officer, case position, linked FIRs, reports awaited (`parts.glance`) |
| 2 | Sherlock's brief | P5 Summary writer | the case in short, what the evidence shows, what does not fit, what is still missing, Sherlock's view, next steps - short points |
| 3 | The story of the case | P7 Story writer | the case in the order it happened: incident, FIR, arrests, disclosures and recoveries, statements, reports sent and received and what they found, where it stands - each step with its source (`parts.case_story.events`) |
| 4 | The target | P2 Target writer | identity (a number registered on another person's CNIC is said once, next to the number - never a lead), part and status in this case, where the records put them in the incident window, their OWN earlier FIRs, their close circle (family, co-accused, the same vehicle, travel, phone contact) (`parts.target`) |
| 4a | The target's record (only while the case is not described) | P8 Record writer | earlier cases (criminal record), people connected to the target (grouped: family, named together in FIRs, phones, the same address or area, hotel stays, tenancy / PRVS / TRUST / SBVS records, vehicles, work), hotel history with check-in and check-out, what the earlier reports say (FIR, heading, what it says) (`parts.record`) |
| 5 | People in this case | P7 Story writer | each person this case's records name: part, status, what they did or said (`parts.case_story.people`) |
| 6 | Statements and what does not fit | P3 Statement writer | what each person of the case said, what changed, contradictions with the evidence (`parts.in_case.said`, `contradictions`) |
| 7 | Medical, forensic and laboratory reports | P2 Evidence writer | each report of this case, custody gaps, reports awaited (`parts.in_case.reports`) |
| 8 | Leads worth checking | P4 Analysis writer | only leads about the case's people; left out of the court-file version (`parts.in_case.leads`) |
| 9 | Next steps for the investigating officer | P1 Report editor | what to do now, the questions waiting (each once) |
| - | Annex | P1 Report editor | the police systems checked for the target; the records the report rests on |

Order of work: `freeze -> scope -> [evidence | statements | analysis] -> editor -> target ->
record -> story -> summary -> check -> (summary once more) -> build`. P6 checks every id.

**No internal ids in the PDF.** The board keeps its ids (D1, W2, X3) and P6 checks them, but
the PDF never prints one: a record is named by what it is ("FIR 77/24 · PS Saddar",
"statement of Asif, 06-06-2023"), the analysts' own entries (contradictions, leads, lines of
inquiry) are not cited, and the annex lists the records the report rests on
(`report_pdf._Sources`).

**What belongs to the case** is decided by code from the records' structure, never their
words (`team/case_scope.py`): this case's FIR, any FIR file that gives this FIR's number
(the recovery FIR of the weapon), the reports that name those FIRs, the officer's uploads
and notes, and the people those FIRs name. Everything else stays on the board but is not
in the report: a police officer who is complainant here and a witness in fifty other FIRs
does not bring those cases, or the statements in them, into this one.

Rules the report keeps, by code:

* a SIM registered on another person's CNIC (in Pakistan usually a relative's or a
  friend's), a tenancy witness, a landlord and a PRVS guarantor are **not leads, not
  evidence and not the target's circle** (`case_scope.NOT_EVIDENCE`): the lead spotter,
  Sherlock's hypotheses and the hypothesis matrix leave them out;
* a line of inquiry, a summary point or a next step that rests only on an **earlier FIR**
  (by where its entries come from, the FIR number it names, or that FIR's date) is never
  shown as a view on the case under investigation - unless the officer said this case IS
  that FIR, or the FIR names it (`report_graph.past_only`, `names_past_fir`);
* police systems are cited by name ([Hotel Eye], not [HOTEL_EYE]); raw field names are
  written as words;
* a connection between people is said in the records' words - never "inferred" or
  "corroborated", never with a score; the same address means the same house or the same
  area, never only the same city (`weak_links.address_score`, `record_writer.plainly`).

## The report's summary

Short points under fixed headings (`report_graph.SUMMARY`): the case in short (the story
writer's, else the FIR's own record), what the evidence shows, what does not fit, what is
still missing, Sherlock's view (in the court-file version: the lines of inquiry), next
steps.

While the officer has not said what case is being investigated (`report_graph.declared`),
**nothing is assumed about it**: "the case in short" is one fixed sentence saying so, "what
the evidence shows" is empty, "Sherlock's view" says no line of inquiry is weighed, the
hypotheses are not even shown to the writer - all by code. The report then holds what can
always be said - the target's record (P8, `team/record_writer.py`): each earlier FIR with
the date and time of the incident and where it stands (the outcome only from the case
positions, the investigation result and the systems' status), what the complainant said
and what was found against the target; every person the records tie to the target and how
(a tenancy, PRVS, TRUST or SBVS witness, a SIM owner and a shared address are relations
here - never leads); the hotel history; and what each earlier report says. "The case in
short" adds the record in a few lines after the fixed sentence.
Checked on the office model (2026-10-19): it completes a first name in Urdu ("نوید") into
a full name from any list of names it is given, so P8 gives it single name words to spell
by (`name_spellings`), never full names; and it answers the outcome's words with a sentence,
so code keeps the model's words only when they are short English that name the outcome -
otherwise the record's own status, with an Urdu status given in English.

## Kept tidy as the graph grows

* One live fact per graph pattern: "links 2 groups", then 3, then 4 is one finding - the
  older statements are marked replaced (`graph_analyst.restated`), the lead follows the
  latest (`leads.follow`), and a second lead on the same pattern is closed quietly.
* At most four casework questions wait for the officer at once (`questions.OPEN_AT_ONCE`);
  a question whose reason is gone is withdrawn (`questions.withdraw`).
* A name read in a person's own police record, or in the note written from the graph, is
  that person - never "is X the same person as X?".
* A model answer cut off at the token limit is used up to its last complete value
  (`agents.ollama.close_truncated`) instead of being thrown away.
