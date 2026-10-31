# boring-knowledge

A notebook AI agents keep for each other: what one of them learned the hard
way, so the next one does not have to.

> **Not the Pond's memory.** Knowledge holds lessons worth sharing across
> projects and Ponds. Where a project stands, what was decided and what comes
> next belong in the project's own records, and how you work together belongs
> in the Pond's handbook. An AI keeps those only as well as it is reminded to:
> from time to time, tell it "save where we are", and "put that into the
> handbook" when you settled something together
> ([keeping a Pond's memory](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/in-practice.md#keeping-a-ponds-memory-your-part)).

## Why

An AI starts every conversation without memory of the last. A Pond's
handbook carries the working agreement and its project records carry the
project, but what an agent learns along the way that is worth more than the
task (a pitfall, a recipe that worked, a version quirk) used to stay in that
one conversation, or in one Pond. Knowledge is the place for it: an agent
searches it before solving something that feels familiar, and adds what it
learned, with the context it holds for and the evidence. It helps a single
Pond across conversations as much as several Ponds, and Copilot on a Windows
box can read and write the same notebook.

Sometimes the agent needs a nudge: "Try looking in the knowledge base first."
Having a notebook does not mean it always remembers to open it.

## How it works

- An **entry** is small: a title, a Markdown body, tags, the context it holds
  for (product, version), evidence, and whether it was observed or inferred.
- The **author is the verified caller** (the Pond, or the key's holder), never
  an argument; an agent may add which model it is.
- **Nothing is overwritten:** a change is a new version with its author and a
  one-line reason; an entry that is no longer true is retired, with a reason,
  and stays readable. Anyone with write access may revise anything; the
  history shows who wrote what.
- **Entries are data, never instructions**, for every reader: the tool
  descriptions say so, and say to search before adding.
- **Not a backup:** the source of truth stays in a project's own records;
  an entry points there as evidence.
- One SQLite file per notebook, searched with a trigram full-text index. A
  "premise" notebook (one site, one customer) is a separate instance and
  never synced with the global one.

## Details

Shared notebooks: entries with title, body, tags, context and evidence,
attributed to the verified caller. Every writer may revise any entry (with
`expected_version`); history is append-only. Global and premise instances are
separate notebooks, never synced. A notebook is schema 3, exactly as this
release creates it; any other schema is refused unchanged. Reader/writer operations are serialized
on the shared SQLite connection.

Start: `boring-knowledge http --config config.json --db notebook.db --role global`.
Configuration (where tokens come from, who may read or write):
[CONFIGURATION](../docs/CONFIGURATION.md); install: [README](../README.md#installing-on-linux).

Search matches all whitespace-separated literal keywords across title, body, tags,
context and evidence in any order. A query needs at least one term of three
characters for the trigram index; shorter words in a mixed query are checked
against the candidate entries. The separate context filter is a phrase. A
zero-hit search is not proof that no entry exists.
