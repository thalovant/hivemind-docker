"""Make a malformed intent context fail as ``MalformedSession``.

``Session.deserialize`` documents, and ``SessionManager`` relies on, one
failure mode for a bad carrier: ``MalformedSession``. The ``ovos.session.sync``
handler catches exactly that, logs it, and carries on with ``inbound = None``.

The intent-context parser does not honour that contract. Against 2.11.18a1 a
peer sending ``{"context": 5}``, ``{"context": {"frame_stack": "x"}}`` or a
frame whose entities are not a mapping raises ``AttributeError``/``TypeError``/
``ValueError`` straight out of the parser. Those sail past the one handler that
exists and surface on the bus client's receive thread, so a single peer's bad
frame takes the transport down instead of dropping the one message.

Carried from thalovant/hivemind-docker#8, which fixed it in a vendored copy of
the client. The vendored copy is gone; this patches the installed one so the
fix does not depend on re-forking. Retire it once the guard is in a release.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path


PACKAGE = "ovos_bus_client.session"

# Anchor on the INSERTION POINT -- the one call that must be guarded -- not on
# the surrounding block. A patch anchored on a whole body breaks the moment
# upstream adds an unrelated statement next to it, and then silently protects
# nothing.
#
# Matched as a WHOLE LINE, never as a substring. The anchor carries its own
# indentation, and a substring test would also match the same call indented
# more deeply -- splicing the guard into the middle of that deeper indentation
# and leaving a file that no longer parses.
ANCHOR_LINE = '        context = IntentContextManager.deserialize(data.get("context", {}))'

GUARDED = '''        try:
            context = IntentContextManager.deserialize(data.get("context", {}))
        except (AttributeError, TypeError, ValueError) as error:
            # The carrier's own contract: a malformed session is rejected as
            # MalformedSession, which SessionManager already catches. Letting
            # the parser's raw error escape kills the receive thread instead.
            raise MalformedSession(
                f"session carries a malformed intent context: {error}"
            ) from error
'''


def main() -> None:
    spec = importlib.util.find_spec(PACKAGE)
    if spec is None or spec.origin is None:
        raise SystemExit(f"{PACKAGE} is not installed")

    path = Path(spec.origin)
    source = path.read_text()

    if GUARDED in source:
        return

    lines = source.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.rstrip("\n") == ANCHOR_LINE]
    if len(hits) != 1:
        raise SystemExit(
            f"expected exactly one intent-context deserialize anchor in {path}, "
            f"found {len(hits)}. Upstream moved it: check whether the guard is "
            "now applied upstream and drop this patch, or re-anchor it."
        )

    lines[hits[0]] = GUARDED
    path.write_text("".join(lines))


def verify() -> None:
    """Prove the guard holds, here, rather than discovering it in production.

    This runs in a FRESH interpreter on purpose. ``find_spec`` above imports the
    parent package, whose ``__init__`` already pulls in this submodule, so an
    in-process import would hand back the pre-patch module from ``sys.modules``
    and report a failure the patch had in fact fixed.
    """
    check = textwrap.dedent(
        """
        from ovos_bus_client.session import MalformedSession, Session

        for payload in (
            {"context": 5},
            {"context": {"frame_stack": "not-a-list"}},
            {"context": {"frame_stack": [["x", 1]]}},
            {"context": {"frame_stack": [{"entities": "nope"}]}},
        ):
            try:
                Session.deserialize(payload)
            except MalformedSession:
                continue
            except Exception as error:
                raise SystemExit(
                    f"a malformed intent context still escapes as "
                    f"{type(error).__name__}: {payload!r}"
                )
            raise SystemExit(f"a malformed intent context did not raise: {payload!r}")

        # A well-formed carrier must be untouched by the guard.
        assert Session.deserialize({"session_id": "ok", "context": {}}).session_id == "ok"
        assert Session.deserialize({"session_id": "bare"}).session_id == "bare"
        """
    )
    result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            "the MalformedSession guard did not take:\n"
            + (result.stderr or result.stdout).strip()
        )


if __name__ == "__main__":
    main()
    verify()
