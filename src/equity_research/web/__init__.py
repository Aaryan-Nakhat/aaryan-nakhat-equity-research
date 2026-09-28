"""The local web UI and the job API behind it (``eqr serve``, or hosted by the email bot).

One process owns the DuckDB database: the web server, the job runner and (when email is configured)
the email bot all run inside it, and the ``eqr`` CLI forwards its commands here when a server is
running — so the single-writer lock is never contended across processes."""
