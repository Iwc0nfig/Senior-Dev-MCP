# Curated facts

Facts a model reads from the client's code and SQL, one JSON file per packet. `prep.verify`
keeps only the facts whose citations open at the cited line, and writes them to
`*.verified.json`; only those enter the index.

The two files here are **fictional examples** of the format. A real deployment holds a few
dozen packets about the client's own database, and they stay with the client.
