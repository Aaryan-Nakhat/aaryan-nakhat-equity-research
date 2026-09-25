"""The command layer: ``app.handle_request`` routes every workbench command (stock names, screeners,
sectors, Tailwind, Pickaxe, …) and replies via ``reports.email.send_report``. The email bot, the
``eqr`` CLI and the web UI all drive this same entry point, so they behave identically."""
