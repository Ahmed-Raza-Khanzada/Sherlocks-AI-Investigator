"""Link graph: one CNIC or phone in, a person/system relationship graph out.

The pipeline, in the order it runs:

1. ``backends``   - query every police/government system for one person
                     (cdr_report_app's adapters, live or over demo data)
2. ``extractors`` - turn each system's answer into a :class:`SystemRecord`: the
                     person's own attributes, images, flags, and every *other*
                     person that record names
3. ``graph``      - resolve those people into nodes (CNIC is identity) and draw
                     strong edges person -> system -> person
4. ``engine``     - breadth-first: search each newly found person again, until the
                     requested depth or the query budget runs out
5. ``weak_links`` - inferred person <-> person links (address, family, co-stay,
                     OSINT). Always labelled weak; never merged into identity.
"""
