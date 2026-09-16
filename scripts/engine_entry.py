"""Frozen CLI entry: the bundled runtime never needs system Python."""
import os
import sys

import certifi

# A frozen Homebrew Python otherwise looks for certificates on the build Mac.
# Keep hostname/certificate validation enabled with the bundled Mozilla roots.
os.environ["SSL_CERT_FILE"] = certifi.where()

if "--bundle-self-test" in sys.argv:
    import json
    import ssl
    import sqlite3
    from codex_alert import __version__

    context = ssl.create_default_context()
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()["x509_ca"] > 0
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
    print(json.dumps({"version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
                      "tls_roots": True, "sqlite": True}))
else:
    from codex_alert.alert import main
    raise SystemExit(main())
