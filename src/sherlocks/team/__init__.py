"""The agent teams, orchestrated with LangGraph (docs/SHERLOCK_AGENTS.md).

* ``sherlock_graph`` - the Sherlock team's background run on one case board:
  S10 register -> A5 entities -> S5 claims -> S6 / S7 / S8 -> S2 -> S1 -> S9 -> S1 -> S4 -> Q1, Q3.
* ``officer_graph``  - one chat turn of the Officer team: O0 reads the message, O3 records,
  O1 works the question with the shared toolbox (the model picks the tools), O2 checks,
  O5 presents, and the desks add at most one finding or question.

Every agent that has to understand language asks the model with a typed schema; code
only stores, compares structured values and guards (quotes exist, ids exist, budgets).
"""
